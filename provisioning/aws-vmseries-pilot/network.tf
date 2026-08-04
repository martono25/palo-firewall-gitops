# Minimal pilot VPC: one public mgmt subnet + one public dataplane subnet, an
# internet gateway, and a default route. Enough for the firewall's mgmt interface
# to reach the internet (and thus SCM) for onboarding. Standard AWS — easy to
# read and to `destroy`.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project, Project = var.project }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.project, Project = var.project }
}

resource "aws_subnet" "mgmt" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.mgmt_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${var.project}-mgmt", Project = var.project }
}

resource "aws_subnet" "dataplane" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.dataplane_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${var.project}-dataplane", Project = var.project }
}

# ── Dataplane subnets for ethernet1/2 .. ethernet1/4 ──────────────────────
# WHY THESE EXIST. The SCM folder variables map $eth-internet -> ethernet1/3 and
# $eth-local -> ethernet1/4, but on AWS a VM-Series interface only exists if an
# ENI is attached at the matching device index (eth0=mgmt, eth1=ethernet1/1,
# ethN=ethernet1/N). With only two ENIs, ethernet1/3 and ethernet1/4 were
# present in config but had NO hardware behind them — placeholder MACs
# (ba:db:ad:ba:db:03/04), link down, unable to pass traffic.
#
# index 2 is created only to keep the device indexes CONTIGUOUS. PAN-OS is
# documented to map by index, so a gap ought to be fine, but "ought to be" is
# what has cost this project time twice; one spare ENI is cheaper than finding
# out. It is deliberately unconfigured on the firewall.
resource "aws_subnet" "untrust" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.untrust_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${var.project}-untrust", Project = var.project }
}

resource "aws_subnet" "trust" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.trust_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "${var.project}-trust", Project = var.project }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.project}-public", Project = var.project }
}

resource "aws_route_table_association" "mgmt" {
  subnet_id      = aws_subnet.mgmt.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "dataplane" {
  subnet_id      = aws_subnet.dataplane.id
  route_table_id = aws_route_table.public.id
}

# untrust ($eth-internet) faces the internet, so it takes the public route.
#
# SUPERSEDED while the traffic test runs: a subnet may have exactly ONE route
# table association, and the test needs untrust on its own table carrying a
# return route to trust via the firewall. Putting that return route on the
# SHARED public table instead would divert mgmt->trust traffic through the
# firewall too, which is not what is under test.
resource "aws_route_table_association" "untrust" {
  count          = var.enable_traffic_test ? 0 : 1
  subnet_id      = aws_subnet.untrust.id
  route_table_id = aws_route_table.public.id
}

# trust ($eth-local) is the INSIDE. Deliberately NOT on the public route table:
# an internal segment that can reach the internet without traversing the
# firewall would make any policy test meaningless.
resource "aws_route_table" "trust" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-trust", Project = var.project }
}

resource "aws_route_table_association" "trust" {
  subnet_id      = aws_subnet.trust.id
  route_table_id = aws_route_table.trust.id
}

# Mgmt: restrict to your CIDR (HTTPS + SSH). Egress open so it can reach SCM.
resource "aws_security_group" "mgmt" {
  name_prefix = "${var.project}-mgmt-"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS mgmt"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.mgmt_allowed_cidr]
  }
  ingress {
    description = "SSH mgmt"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.mgmt_allowed_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-mgmt", Project = var.project }
}

# Dataplane: the FIREWALL is the policy point on these interfaces, not this
# security group.
#
# This carried ZERO ingress rules until 2026-08-04, with the note "no ingress
# needed for the onboarding pilot". That was true while the dataplane carried
# nothing — and silently stopped being true the moment the interfaces were
# addressed. AWS dropped every inbound packet at the trust/untrust ENIs, so
# traffic never reached PAN-OS at all: rule hit counts stayed at zero across
# every rule INCLUDING interzone-default, which looks identical to "no traffic
# was sent". The firewall's own pings worked throughout, because egress is
# allowed and security groups are stateful — that asymmetry was the tell.
#
# A firewall dataplane interface has to accept traffic addressed elsewhere; that
# is its entire job, and `source_dest_check = false` on the ENIs exists for the
# same reason. Filtering here would mean two policy points disagreeing, with the
# AWS one invisible to the platform and to every intent.
#
# Scoped to the VPC rather than 0.0.0.0/0: untrust sits in a subnet with an IGW
# route, and there is no reason to accept unsolicited traffic from the internet
# at the ENI level even though these interfaces carry no public IP.
resource "aws_security_group" "dataplane" {
  name_prefix = "${var.project}-dataplane-"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "Traffic the firewall is meant to inspect. PAN-OS policy decides, not this."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-dataplane", Project = var.project }
}
