#!/bin/sh
set -eu

network_name=${1:-anythingllm_default}
network_id=$(docker network inspect --format '{{.Id}}' "$network_name")
bridge_name=$(docker network inspect --format '{{ index .Options "com.docker.network.bridge.name" }}' "$network_name")
egress_interface=$(ip route show default | awk '$1 == "default" { print $5; exit }')

if [ -z "$bridge_name" ]; then
  bridge_name="br-$(printf '%s' "$network_id" | cut -c1-12)"
fi

if [ -z "$egress_interface" ]; then
  echo 'Could not determine the host default-route interface' >&2
  exit 1
fi

iptables -C DOCKER-USER -i "$bridge_name" -o "$egress_interface" -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i "$bridge_name" -o "$egress_interface" -p tcp --dport 443 -j ACCEPT
iptables -C DOCKER-USER -i "$egress_interface" -o "$bridge_name" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i "$egress_interface" -o "$bridge_name" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
printf 'Enabled outbound forwarding for Docker network %s (%s) through %s\n' "$network_name" "$bridge_name" "$egress_interface"
