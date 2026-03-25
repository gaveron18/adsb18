#!/bin/bash
# Install adsb18 feeder as systemd service on Raspberry Pi
# Usage: sudo bash install.sh --server 173.249.2.184 --name perm-pi5

SERVER=""
NAME=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --server) SERVER="$2"; shift 2 ;;
        --name)   NAME="$2";   shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [[ -z "$SERVER" ]]; then
    echo "Usage: sudo bash install.sh --server <IP> --name <feeder-name>"
    exit 1
fi

NAME="${NAME:-$(hostname)}"
INSTALL_DIR="/opt/adsb18-feeder"

echo "Installing adsb18 feeder..."
echo "  Server: $SERVER"
echo "  Name:   $NAME"

mkdir -p "$INSTALL_DIR"
cp feeder.py "$INSTALL_DIR/"

cat > /etc/systemd/system/adsb18-feeder.service << EOF
[Unit]
Description=adsb18 ADS-B Feeder
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $INSTALL_DIR/feeder.py --server $SERVER --name $NAME --buffer $INSTALL_DIR/feeder_buffer.sbs
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable adsb18-feeder
systemctl start  adsb18-feeder
echo "Done. Status:"
systemctl status adsb18-feeder --no-pager
