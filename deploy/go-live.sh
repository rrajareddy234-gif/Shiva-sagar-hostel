#!/usr/bin/env bash
#
# One-shot setup for a fresh Ubuntu server.
#
# Run this on the server, as a user with sudo. It installs Docker, clones the
# code, and starts the stack. It does NOT touch secrets: .env is created with
# blanks and the script stops so you can fill it in yourself.
#
#   bash go-live.sh
#
set -u

REPO="https://github.com/rrajareddy234-gif/Shiva-sagar-hostel.git"
BRANCH="fix/remove-unused-imports"
APP_DIR="/opt/sagar"
COMPOSE="docker compose -f deploy/docker-compose.lite.yml"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as a normal user, not root. It uses sudo where needed."
    exit 1
fi

say "1/7  Swap file (stops a 1 GB machine choking)"
if [ -f /swapfile ]; then
    echo "    already present, skipping"
else
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "    2 GB swap added"
fi

say "2/7  Docker"
if command -v docker >/dev/null 2>&1; then
    echo "    already installed, skipping"
else
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io docker-compose-v2 git
    sudo usermod -aG docker "$USER"
    echo "    installed"
fi

# The group change above only applies to a new login. Re-running this script is
# safe, so the cleanest fix is to stop and ask for a reconnect.
if ! docker info >/dev/null 2>&1; then
    cat <<'EOF'

    Docker is installed but this shell is not in the docker group yet.

    Close this SSH window, open a new one, and run the same command again.
    Nothing is lost: this script picks up where it left off.

EOF
    exit 0
fi

say "3/7  Code"
if [ -d "$APP_DIR/.git" ]; then
    echo "    already cloned, pulling"
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" checkout --quiet "$BRANCH"
    git -C "$APP_DIR" pull --quiet origin "$BRANCH"
else
    sudo mkdir -p "$APP_DIR"
    sudo chown "$USER" "$APP_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
echo "    at $(git -C "$APP_DIR" rev-parse --short HEAD)"

say "4/7  Secrets file"
if [ -f "$APP_DIR/.env" ]; then
    echo "    .env already exists, leaving it alone"
else
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "    created $APP_DIR/.env with blanks"
fi

# The hostname Caddy uses for HTTPS. sslip.io resolves any name of the form
# <ip>.sslip.io to that ip, so this works with no domain purchase.
IP="$(curl -s -m 10 -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
if [ -n "$IP" ]; then
    if grep -q '^SAGAR_HOST=' "$APP_DIR/.env"; then
        sed -i "s|^SAGAR_HOST=.*|SAGAR_HOST=${IP}.sslip.io|" "$APP_DIR/.env"
    else
        printf '\nSAGAR_HOST=%s.sslip.io\n' "$IP" >> "$APP_DIR/.env"
    fi
    echo "    SAGAR_HOST set to ${IP}.sslip.io"
fi

say "5/7  Stop here and fill in the secrets"
cat <<EOF
    Run this, fill in the blanks, save and exit (Ctrl+O, Enter, Ctrl+X):

        nano $APP_DIR/.env

    Fill in only these lines:

        POSTGRES_PASSWORD=<make one up, long>
        META_APP_SECRET=<from Meta>
        META_ACCESS_TOKEN=<from Meta>
        META_PHONE_NUMBER_ID=<from Meta>
        META_VERIFY_TOKEN=<make one up>
        OWNER_PHONE=+918688397996
        DASHBOARD_PASSWORD=<make one up>
        AI_API_KEY=<from Google AI Studio>
        JOB_TOKEN=<see below>
        SAGAR_HOST=<the value already filled in above>

    Generate the job token with:

        python3 -c "import secrets; print(secrets.token_urlsafe(32))"

EOF

if [ ! -t 0 ]; then
    echo "    (not a terminal, so stopping here)"
    exit 0
fi

read -r -p "    Press Enter once .env is filled in and saved... " _

say "6/7  Build and start"
cd "$APP_DIR" || exit 1
$COMPOSE up -d --build

say "7/7  Database"
$COMPOSE exec -T postgres psql -U sagar -d sagarpg < db/schema.sql
$COMPOSE exec -T postgres psql -U sagar -d sagarpg < db/seed.sql

printf '\n\033[1mDone.\033[0m Check it with:\n\n'
HOST="$(grep '^SAGAR_HOST=' "$APP_DIR/.env" | cut -d= -f2)"
echo "    curl https://${HOST}/health"
echo "    $COMPOSE ps"
echo
