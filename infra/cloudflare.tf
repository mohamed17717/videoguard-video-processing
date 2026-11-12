# Cloudflare DNS for the playback hostname.
#
# The record is a CNAME from the operator-chosen hostname (var.pull_zone_hostname,
# e.g. "media") to the Bunny pull zone's default hostname
# (<zone-name>.b-cdn.net).
#
# proxied = false ON PURPOSE: Bunny terminates TLS itself and issues its own
# certificate for the custom hostname; double-proxying through Cloudflare
# would break SNI/hostname verification and double-bill delivery. The token
# (variables.tf, sensitive) only needs DNS-edit rights on this zone.
#
# DEFERRED GATES (CONTRACT.md section 11): real DNS apply, Bunny custom
# hostname certificate issuance, and the end-to-end playback flow are not
# verifiable without real accounts.

resource "cloudflare_record" "pull_zone_cname" {
  zone_id = var.cloudflare_zone_id
  name    = var.pull_zone_hostname
  type    = "CNAME"
  content = "${local.bunny_zone_name}.b-cdn.net"
  proxied = false
  ttl     = 1
}
