# EAST-WEST TRAFFIC PROOF — two disposable hosts either side of the firewall.
#
# Everything about this pilot is proven at CONFIGURATION level: interfaces,
# zones and routes are live on the device, driven from YAML intent. What has
# never been shown is a PACKET crossing the firewall. This is the smallest thing
# that shows it.
#
# WHY EAST-WEST AND NOT INTERNET EGRESS. Sending trust traffic to the internet
# needs source-NAT on the firewall, and NatRequest is deferred to v2.0 and
# unimplemented. Hand-configuring NAT would prove the firewall works but not that
# the PIPELINE does, which is the claim under test. East-west needs no NAT.
#
# THE AWS CONSTRAINT THAT SHAPES THIS. Inside a VPC the `local` route
# (10.100.0.0/16) normally wins, so trust -> untrust would go DIRECT and never
# reach the firewall — the test would pass while proving nothing. AWS permits a
# route MORE SPECIFIC than local to point at an ENI for exactly this appliance
# case, so each side gets a /24 route at the firewall's interface on its own
# side. That specificity is what forces the packet through the box.
#
# Set `enable_traffic_test = false` to remove all of it.

variable "enable_traffic_test" {
  description = "Launch the two disposable east-west test hosts. Off by default."
  type        = bool
  default     = false
}

data "aws_ssm_parameter" "al2023" {
  count = var.enable_traffic_test ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ── Routing: force trust <-> untrust through the firewall ─────────────────
# More specific than the VPC local route, so it wins.
resource "aws_route" "trust_to_untrust_via_fw" {
  count                  = var.enable_traffic_test ? 1 : 0
  route_table_id         = aws_route_table.trust.id
  destination_cidr_block = var.untrust_subnet_cidr
  network_interface_id   = module.vmseries.interfaces["trust"].id
}

# untrust gets its OWN table: it currently shares the public one with mgmt, and a
# return route added there would divert mgmt traffic through the firewall too.
resource "aws_route_table" "untrust" {
  count  = var.enable_traffic_test ? 1 : 0
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  # NO VPC-level return route here, deliberately.
  #
  # There was one (trust_subnet_cidr -> the firewall's untrust ENI) and it broke
  # the FORWARD path. The firewall's untrust interface is itself in this subnet,
  # so it uses this table: forwarding the client's SYN — source 10.100.3.200 —
  # meant AWS saw traffic whose source subnet routed straight back to the sending
  # ENI. The SYN left the firewall (proven in tx.pcap) and never reached the
  # target (PassiveOpens stayed at 0 there).
  #
  # It was also redundant: the target carries a HOST route
  # `10.100.3.0/24 via 10.100.2.142`, which returns traffic through the firewall
  # without involving this table at all.
  tags = { Name = "${var.project}-untrust", Project = var.project }
}

resource "aws_route_table_association" "untrust_test" {
  count          = var.enable_traffic_test ? 1 : 0
  subnet_id      = aws_subnet.untrust.id
  route_table_id = aws_route_table.untrust[0].id
}

# ── Security groups: permissive, because the FIREWALL is what is under test ──
# An AWS SG denying this traffic would mask the thing being measured.
resource "aws_security_group" "test_host" {
  count       = var.enable_traffic_test ? 1 : 0
  name_prefix = "${var.project}-testhost-"
  vpc_id      = aws_vpc.this.id
  description = "Disposable east-west test hosts. The firewall is the control point, not this."

  ingress {
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = [var.vpc_cidr]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-testhost", Project = var.project }
}

# ── The two hosts ─────────────────────────────────────────────────────────
# Neither subnet can reach the internet, so there is no SSH and no SSM. Results
# come back through the EC2 console log, which needs no inbound access at all.
resource "aws_instance" "untrust_target" {
  count                       = var.enable_traffic_test ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023[0].value
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.untrust.id
  vpc_security_group_ids      = [aws_security_group.test_host[0].id]
  private_ip                  = "10.100.2.200"
  associate_public_ip_address = false

  user_data = <<-EOT
    #!/bin/bash
    set -x
    # HOST ROUTE for the RETURN path. The firewall's untrust interface
    # (10.100.2.142) is in THIS subnet, so it is reachable by ARP and can be a
    # next-hop directly. Without this the reply goes back via the AWS router and
    # the firewall sees a one-way flow, which it drops as asymmetric.
    ip route replace 10.100.3.0/24 via 10.100.2.142 dev ens5

    # Serve something identifiable so a successful fetch cannot be confused with
    # a cached or local response.
    #
    # A systemd unit, NOT `nohup ... &`: cloud-init reaps its children when the
    # script exits, so a backgrounded server dies with it and the port is never
    # open. curl then returns 000, which is indistinguishable from the firewall
    # blocking the connection.
    #
    # Written with printf rather than a nested heredoc: this file is already
    # inside Terraform's indented <<-EOT, and an inner heredoc gets mangled by
    # the indentation stripping — which failed cloud-init outright
    # ("Failed to run module scripts-user") so NOTHING ran.
    #
    # No dnf: this host has no route to the internet, so package installation
    # cannot work. python3 ships with AL2023.
    mkdir -p /var/www-index
    echo "FWGITOPS-TARGET-OK" > /var/www-index/index.html
    printf '%s\n' \
      '[Unit]' \
      'Description=fwgitops traffic-proof target' \
      'After=network-online.target' \
      '[Service]' \
      'WorkingDirectory=/var/www-index' \
      'ExecStart=/usr/bin/python3 -m http.server 80' \
      'Restart=always' \
      '[Install]' \
      'WantedBy=multi-user.target' \
      > /etc/systemd/system/fwgitops-target.service
    systemctl daemon-reload
    systemctl enable --now fwgitops-target.service
    sleep 3
    # Answer "is the port actually open" in the test rather than assuming it.
    { ip route show; ss -lntp | grep ':80' || echo "PORT-80-NOT-LISTENING"; } > /dev/console 2>&1

    # Does the SYN actually ARRIVE here? The firewall's capture proves it
    # FORWARDS one and never sees a reply, which cannot distinguish "target never
    # got it" from "target replied and the reply was lost". Only this side can.
    #
    # CUMULATIVE kernel counters, not `ss`: SYN_RECV lasts seconds, so a 20s poll
    # of the socket table misses it and reports zero either way. PassiveOpens
    # counts every SYN that reached the listener; AttemptFails/InErrs catch the
    # rest. These only ever increase, so nothing is missed between samples.
    nohup sh -c 'while true; do
      awk "/^Tcp:/{h=\$0; getline; print \"TARGET-TCP \" \$6 \" passive=\" \$6 \" attemptfail=\" \$7 \" estabresets=\" \$8 \" insegs=\" \$11}" /proc/net/snmp > /dev/console 2>&1
      sleep 20
    done' >/dev/null 2>&1 &
    disown
  EOT

  tags = { Name = "${var.project}-untrust-target", Project = var.project }
}

resource "aws_instance" "trust_client" {
  count                       = var.enable_traffic_test ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023[0].value
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.trust.id
  vpc_security_group_ids      = [aws_security_group.test_host[0].id]
  private_ip                  = "10.100.3.200"
  associate_public_ip_address = false
  depends_on                  = [aws_instance.untrust_target]

  # Writes to the console so results are readable without any inbound path.
  user_data = <<-EOT
    #!/bin/bash
    exec > /dev/console 2>&1

    # HOST ROUTE — the point of this revision.
    #
    # Both hosts default to the AWS VPC router (10.100.3.1 / 10.100.2.1), so the
    # earlier design leaned entirely on a VPC route table redirecting
    # 10.100.2.0/24 to the firewall's ENI. That redirect never delivered: over 11
    # attempts ethernet1/4 moved ONE packet and every rule, including
    # interzone-default, stayed at zero hits.
    #
    # The firewall's trust interface (10.100.3.125) is in THIS subnet, so it can
    # be a next-hop directly by ARP. That takes the VPC route table out of the
    # path entirely rather than needing to understand why it did not work.
    # source_dest_check is already false on the firewall ENIs, which is what
    # lets it forward traffic not addressed to itself.
    ip route replace 10.100.2.0/24 via 10.100.3.125 dev ens5
    echo "=== ROUTES ==="; ip route show
    echo "=== next hop for the target ==="; ip route get 10.100.2.200

    sleep 45
    echo "=== FWGITOPS TRAFFIC TEST: 10.100.3.200 -> 10.100.2.200 via the firewall ==="
    # Runs long enough to observe live on the firewall (session table, rule hit
    # counts). A short burst finishes before anything can be watched, which is
    # how the first run left "did it even arrive?" unanswerable.
    # RUNS FOREVER, on purpose. A bounded loop means re-testing requires
    # recreating the instance, and each replacement gets a new MAC while PAN-OS
    # holds ARP for 1800s — so the firewall keeps forwarding to a dead MAC and
    # every subsequent result is poisoned. That cost hours in this session.
    #
    # An endless loop makes the harness re-usable without touching the
    # instances: change policy, watch the next iteration. If it must be
    # recreated, recreate the CLIENT (its ARP re-resolves when it ARPs for the
    # firewall) and leave the TARGET alone (the firewall initiates to it, so a
    # stale entry there persists for the full ARP lifetime).
    while true; do
      echo "--- $(date +%T) ---"
      echo "ping:"; ping -c 2 -W 2 10.100.2.200 2>&1 | tail -2
      echo "http:"; curl -s -m 5 -o /dev/null -w 'curl_http_code=%%{http_code}\n' http://10.100.2.200/ 2>&1
      sleep 15
    done
  EOT

  tags = { Name = "${var.project}-trust-client", Project = var.project }
}

output "traffic_test" {
  value = var.enable_traffic_test ? {
    client = "10.100.3.200 (trust)"
    target = "10.100.2.200 (untrust)"
    read   = "aws ec2 get-console-output --instance-id ${aws_instance.trust_client[0].id} --output text"
  } : null
}
