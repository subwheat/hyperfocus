#!/bin/bash
set -e
echo "🚀 Hyperfocus DEV1-S Setup"
echo "=========================="

# 1. Generate Authelia password hashes
echo ""
echo "📋 Step 1: Set passwords for Authelia users"
echo "---------------------------------------------"

read -p "Password for julien: " -s JULIEN_PW
echo ""
read -p "Password for nico: " -s NICO_PW
echo ""

echo "Generating hashes..."
JULIEN_HASH=$(docker run --rm authelia/authelia:4 authelia crypto hash generate argon2 --password "$JULIEN_PW" 2>/dev/null | grep 'Digest:' | awk '{print $2}')
NICO_HASH=$(docker run --rm authelia/authelia:4 authelia crypto hash generate argon2 --password "$NICO_PW" 2>/dev/null | grep 'Digest:' | awk '{print $2}')

# Update users database
sed -i "s|\$argon2id.*placeholder_generate_on_server|${JULIEN_HASH}|1" authelia/config/users_database.yml
sed -i "0,/placeholder_generate_on_server/! s|\$argon2id.*placeholder_generate_on_server|${NICO_HASH}|" authelia/config/users_database.yml

echo "✅ Password hashes generated"

# 2. Set Claude API key
echo ""
echo "📋 Step 2: Claude API Key"
echo "-------------------------"
read -p "Claude API Key (sk-ant-...): " CLAUDE_KEY
sed -i "s|REPLACE_WITH_YOUR_CLAUDE_API_KEY|${CLAUDE_KEY}|" .env
echo "✅ API key set"

# 3. Generate self-signed SSL cert (or skip)
echo ""
echo "📋 Step 3: SSL Certificate"
echo "--------------------------"
if [ ! -f nginx/certs/fullchain.pem ]; then
    echo "Generating self-signed cert (replace with Let's Encrypt later)..."
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout nginx/certs/privkey.pem \
        -out nginx/certs/fullchain.pem \
        -subj "/CN=rosetta.chat/O=Uyuni" 2>/dev/null
    echo "✅ Self-signed cert generated"
else
    echo "✅ Certs already exist"
fi

# 4. Build and start
echo ""
echo "📋 Step 4: Build & Launch"
echo "-------------------------"
docker compose build
docker compose up -d
sleep 5

echo ""
echo "📋 Status:"
docker compose ps

echo ""
echo "🎉 Done!"
echo "Access: http://51.159.140.237"
echo "Auth: Authelia login (julien / nico)"
