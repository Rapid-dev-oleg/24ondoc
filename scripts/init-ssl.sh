#!/usr/bin/env bash
set -euo pipefail

# Initial SSL certificate setup with Let's Encrypt
# Run this ONCE on the VPS after DNS is configured
# Usage: ./scripts/init-ssl.sh your-email@example.com

EMAIL="${1:?Usage: $0 email@example.com}"
DOMAINS=("api.24ondoc.ru" "chat.24ondoc.ru")

echo "=== Obtaining SSL certificates ==="

for DOMAIN in "${DOMAINS[@]}"; do
    echo "--- Requesting certificate for $DOMAIN ---"
    docker compose run --rm certbot certonly \
        --webroot \
        --webroot-path=/var/www/certbot \
        --email "$EMAIL" \
        --agree-tos \
        --no-eff-email \
        -d "$DOMAIN"
done

echo "--- Reloading nginx ---"
docker compose exec nginx nginx -s reload

echo "=== SSL setup complete ==="
echo "Certificates will auto-renew via certbot container."
