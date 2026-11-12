# Security group for the ephemeral worker (CONTRACT.md section 8).
#
# NO INGRESS RULES AT ALL. Nothing may open a connection to the worker.
#
# NETWORK HONESTY (CONTRACT.md section 8): a security group can allowlist
# ports and CIDR ranges, but NOT hostnames - and Railway publishes no stable
# IP list for its webhook receiver. The worker needs HTTPS to S3, SQS,
# Secrets Manager, ECR, CloudWatch and Railway. Therefore egress is TCP 443
# to 0.0.0.0/0, and the real boundary is:
#
#   - the EPHEMERAL INSTANCE (terminated after every job, no reused state),
#   - no inbound reachability,
#   - untrusted input parsed only inside the network-less validator and
#     generator containers (--network=none, SECURITY.md section 3),
#   - least-privilege IAM (iam.tf).
#
# Do NOT claim "no 0.0.0.0/0 egress" anywhere in the docs. The alternative
# design (VPC interface endpoints for AWS services + a documented Railway
# egress CIDR) is a later hardening phase and costs ~$7/month per endpoint.

resource "aws_security_group" "worker" {
  name        = "vg-worker-${var.env}"
  description = "video-guard ephemeral worker: egress-only. No inbound rules by design (CONTRACT.md section 8)."
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "HTTPS: S3, SQS, Secrets Manager, ECR, CloudWatch, Docker registry, Railway webhook. Hostname allowlisting is impossible in an SG; the ephemeral instance is the boundary."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
