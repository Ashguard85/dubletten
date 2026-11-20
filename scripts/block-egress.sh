#!/usr/bin/env bash
# Sperrt neu aufgebaute FORWARD-Verbindungen aus der CRM-Docker-Bridge.
# Container->Host-Ollama geht durch INPUT, nicht FORWARD und bleibt erreichbar.
# Nur Docker Engine mit iptables-Backend; kein vollstaendiger Airgap-Nachweis.
set -euo pipefail
NET="crm_dubletten_network"
BR="br-crm-dub"
[[ ${EUID} == 0 ]] || { echo 'Bitte sudo verwenden.' >&2; exit 1; }
command -v docker >/dev/null
command -v iptables >/dev/null
iptables -w -n -L DOCKER-USER >/dev/null 2>&1 || {
  echo 'Keine DOCKER-USER-Kette: Docker nftables-Backend? Regeln manuell pruefen.' >&2
  exit 2
}
ACTUAL="$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NET")"
if [[ "$ACTUAL" != "$BR" ]]; then
  echo "Unerwartete Bridge: $ACTUAL statt $BR" >&2; exit 3
fi
ip link show "$BR" >/dev/null
if iptables -w -C DOCKER-USER -i "$BR" -m conntrack --ctstate NEW -j REJECT 2>/dev/null; then
  echo 'Bereits aktiv.'
else
  iptables -w -I DOCKER-USER 1 -i "$BR" -m conntrack --ctstate NEW -j REJECT
  echo 'NEUE ausgehende FORWARD-Verbindungen gesperrt.'
fi
iptables -w -S DOCKER-USER
