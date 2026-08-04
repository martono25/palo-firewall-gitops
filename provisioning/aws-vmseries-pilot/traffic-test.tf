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
  route {
    # Return path. Without this the reply is routed direct and the firewall sees
    # a one-way flow, which it drops as asymmetric — the test would fail for a
    # reason that has nothing to do with policy.
    cidr_block           = var.trust_subnet_cidr
    network_interface_id = module.vmseries.interfaces["untrust"].id
  }
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
    # Serve something identifiable so a successful fetch cannot be confused with
    # a cached or local response.
    dnf install -y python3 >/dev/null 2>&1
    echo "FWGITOPS-TARGET-OK" > /tmp/index.html
    cd /tmp && nohup python3 -m http.server 80 >/dev/null 2>&1 &
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
    sleep 45
    echo "=== FWGITOPS TRAFFIC TEST: 10.100.3.200 -> 10.100.2.200 via the firewall ==="
    # Runs long enough to observe live on the firewall (session table, rule hit
    # counts). A short burst finishes before anything can be watched, which is
    # how the first run left "did it even arrive?" unanswerable.
    for i in $(seq 1 40); do
      echo "--- attempt $i ---"
      echo "ping:"; ping -c 2 -W 2 10.100.2.200 2>&1 | tail -2
      echo "http:"; curl -s -m 5 -o /dev/null -w 'curl_http_code=%%{http_code}\n' http://10.100.2.200/ 2>&1
      sleep 15
    done
    echo "=== FWGITOPS TRAFFIC TEST END ==="
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
