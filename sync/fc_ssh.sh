#!/usr/bin/env bash
# fc_ssh.sh — run bench commands on a Frappe Cloud PRIVATE bench over SSH, the way FC actually works.
#
#   * certificates come from the press API and live 6 h; only one valid cert per key+group, so we
#     reuse an existing one (press.api.bench.certificate) before minting (generate_certificate)
#   * the cert MUST sit next to the private key as <key>-cert.pub (id_ed25519 → id_ed25519-cert.pub)
#   * username = current Bench name (bench-XXXX-YYYYYY-fN-region; changes on every deploy),
#     host = the cluster proxy (nN-region.frappe.cloud), port 2222
#   * the proxy runs a forced command, so remote-command arguments are IGNORED and scp/rsync fail;
#     the only way to script is to feed the shell on stdin — which is what `run` does.
#
# Env (or GitHub secrets/vars):
#   FC_API_KEY FC_API_SECRET   Frappe Cloud API key (Dashboard → Settings → API Access)
#   FC_TEAM                    team docname (press.api.account.me → message.team)
#   FC_GROUP                   release group, e.g. bench-0001 (dashboard URL)
#   FC_SSH_PRIVATE_KEY         private key text (ed25519 recommended) — OR — FC_SSH_KEY_PATH (default ~/.ssh/id_ed25519)
#   FC_KNOWN_HOSTS             optional pinned known_hosts line(s) for the proxy (ssh-keyscan -p 2222 -H host)
#
# Usage:
#   fc_ssh.sh cert                      # ensure a valid certificate is installed; prints validity
#   fc_ssh.sh target                    # prints "<bench> <proxy>" for the active bench
#   fc_ssh.sh run  < script.sh          # run a script on the bench (stdin), e.g.:
#       printf 'bench version\nbench --site %s execute frappe.db.count --args "[\\"Customer\\"]"\nexit\n' "$FC_SITE" | fc_ssh.sh run
#   fc_ssh.sh shell                     # interactive session
#   fc_ssh.sh sql "select count(*) n from tabItem"   # NO SSH: press SQL playground (read-only unless COMMIT=1)
set -euo pipefail

FC=${FC_URL:-https://frappecloud.com}/api/method
need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "missing env $v" >&2; exit 2; }; done; }
need FC_API_KEY FC_API_SECRET FC_TEAM

auth=(-H "Authorization: Token $FC_API_KEY:$FC_API_SECRET" -H "X-Press-Team: $FC_TEAM" -H "Content-Type: application/json")
press() { # press <method> <json-body>
  curl -fsS "${auth[@]}" -X POST "$FC/$1" -d "${2:-{\}}"
}

key_path=${FC_SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}
cert_path="${key_path}-cert.pub"

install_key() {
  mkdir -p "$(dirname "$key_path")"; chmod 700 "$(dirname "$key_path")"
  if [ -n "${FC_SSH_PRIVATE_KEY:-}" ]; then
    printf '%s\n' "$FC_SSH_PRIVATE_KEY" > "$key_path"; chmod 600 "$key_path"
  fi
  [ -f "$key_path" ] || { echo "no private key at $key_path (set FC_SSH_PRIVATE_KEY or FC_SSH_KEY_PATH)" >&2; exit 2; }
}

ensure_cert() {
  need FC_GROUP
  install_key
  local cert
  cert=$(press press.api.bench.certificate "{\"name\":\"$FC_GROUP\"}" | jq -r '.message.ssh_certificate // empty' || true)
  if [ -z "$cert" ]; then
    cert=$(press press.api.bench.generate_certificate "{\"name\":\"$FC_GROUP\"}" | jq -r '.message.ssh_certificate')
  fi
  [ -n "$cert" ] && [ "$cert" != "null" ] || { echo "could not obtain certificate (team SSH access enabled? default key registered?)" >&2; exit 3; }
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$cert"
  printf '%s\n' "$cert" > "$cert_path"; chmod 644 "$cert_path"
  ssh-keygen -Lf "$cert_path" | grep -E 'Principals|Valid|Type' | sed 's/^/  /' >&2
}

target() {
  need FC_GROUP
  press press.api.client.run_doc_method "{\"dt\":\"Release Group\",\"dn\":\"$FC_GROUP\",\"method\":\"deployed_versions\"}" \
    | jq -r '[.message[] | select(.status=="Active")][0] | "\(.name) \(.proxy_server)"'
}

known_hosts() {
  local proxy=$1
  mkdir -p "$HOME/.ssh"
  if [ -n "${FC_KNOWN_HOSTS:-}" ]; then
    printf '%s\n' "$FC_KNOWN_HOSTS" >> "$HOME/.ssh/known_hosts"
  elif ! ssh-keygen -F "[$proxy]:2222" -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1; then
    echo "  pinning host key via ssh-keyscan (TOFU) — store FC_KNOWN_HOSTS to make this MITM-proof" >&2
    ssh-keyscan -p 2222 -H "$proxy" >> "$HOME/.ssh/known_hosts" 2>/dev/null
  fi
}

ssh_base() {
  read -r bench proxy < <(target)
  [ -n "$bench" ] && [ "$bench" != "null" ] || { echo "no Active bench found in $FC_GROUP" >&2; exit 3; }
  known_hosts "$proxy"
  SSH=(ssh -p 2222 -o IdentitiesOnly=yes -i "$key_path" -o CertificateFile="$cert_path"
       -o StrictHostKeyChecking=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 "$bench@$proxy")
}

case "${1:-}" in
  cert)   ensure_cert ;;
  target) target ;;
  run)
    ensure_cert; ssh_base
    # -T: no tty; stdin script reaches the inner `bash --login` in /home/frappe/frappe-bench.
    { echo 'set -euo pipefail; cd /home/frappe/frappe-bench'; cat; echo; echo 'exit'; } | "${SSH[@]}" -T ;;
  shell)
    ensure_cert; ssh_base; exec "${SSH[@]}" -tt ;;
  sql)
    need FC_SITE
    q=$(jq -Rn --arg q "${2:?sql needed}" '$q')
    press press.api.client.run_doc_method \
      "{\"dt\":\"Site\",\"dn\":\"$FC_SITE\",\"method\":\"run_sql_query_in_database\",\"args\":{\"query\":$q,\"commit\":${COMMIT:-false}}}" \
      | jq '.message' ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
