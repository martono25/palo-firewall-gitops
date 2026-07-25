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

# Dataplane: no ingress needed for the onboarding pilot; egress open.
resource "aws_security_group" "dataplane" {
  name_prefix = "${var.project}-dataplane-"
  vpc_id      = aws_vpc.this.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-dataplane", Project = var.project }
}
