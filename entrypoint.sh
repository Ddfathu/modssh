#!/bin/bash

# Mengambil environment variables atau menggunakan nilai default
USER_NAME="${SSH_USER:-jatim}"
USER_PASS="${SSH_PASSWORD:-jatim}"

# Port PUBLIK (yang di-arahkan Railway TCP Proxy ke sini)
PUBLIC_PORT="${PORT:-8080}"

# Port INTERNAL, tidak diekspos keluar, hanya dipakai antar-proses di dalam container
SSL_INTERNAL_PORT="${SSL_INTERNAL_PORT:-2443}"
WS_INTERNAL_PORT="${WS_INTERNAL_PORT:-8880}"

echo "[*] Mengonfigurasi Server Message Dropbear (Banner Pra-Login)..."
cat << 'EOF' > /etc/dropbear_banner
=================================================
             PREMIUM SSH SERVER DROPBEAR         
=================================================
       Dilarang Torrent / DDOS / Hacking!        
=================================================
EOF

echo "[*] Mengonfigurasi Respon Server (Pasca-Login)..."
cat << 'EOF' > /etc/profile.d/99-respon-server.sh
#!/bin/bash
clear
echo -e "\e[1;36m=================================================\e[0m"
echo -e "\e[1;32m       [✓] BERHASIL TERHUBUNG KE SERVER!         \e[0m"
echo -e "\e[1;36m=================================================\e[0m"
echo -e "\e[1;37m Username     : \e[1;33m$USER\e[0m"
echo -e "\e[1;37m Waktu Server : \e[1;33m$(date)\e[0m"
echo -e "\e[1;37m OS           : \e[1;33mUbuntu 22.04 (Dropbear Mode)\e[0m"
echo -e "\e[1;36m=================================================\e[0m"
echo -e "\e[1;31m   TETAP PATUHI RULES SERVER AGAR TIDAK BANNED   \e[0m"
echo -e "\e[1;36m=================================================\e[0m"
EOF
chmod +x /etc/profile.d/99-respon-server.sh

echo "[*] Mengonfigurasi User SSH..."
if ! id "$USER_NAME" &>/dev/null; then
    useradd -m -s /bin/bash "$USER_NAME"
    usermod -aG sudo "$USER_NAME"
fi
echo "$USER_NAME:$USER_PASS" | chpasswd

echo "[*] Memulai Dropbear Server di Port Lokal 22..."
/usr/sbin/dropbear -p 127.0.0.1:22 -b /etc/dropbear_banner -W 65536

# --- PERBAIKAN FATAL: Mengubah accept ke 0.0.0.0 agar Cloudflare Tunnel Bisa Masuk ---
echo "[*] Membuat konfigurasi Stunnel (internal) di Port $SSL_INTERNAL_PORT..."
cat <<EOF > /etc/stunnel/stunnel.conf
pid = /var/run/stunnel.pid
foreground = yes
debug = 4

[ssh-ssl]
accept = 0.0.0.0:$SSL_INTERNAL_PORT
connect = 127.0.0.1:22
cert = /etc/stunnel/stunnel.pem
EOF

echo "[*] Menambahkan sesuatu di .bashrc..."
cat <<'EOF'>> ~/.bashrc
clear
R='\e[1;31m'
G='\e[1;32m'
C='\e[1;36m'
N='\e[0m'

alias c='clear'
alias x='exit'
alias +x='chmod +x'
alias cls='clear;ls'

menu
EOF

echo "[*] Memulai Stunnel (internal, port $SSL_INTERNAL_PORT)..."
stunnel /etc/stunnel/stunnel.conf &

echo "[*] Memulai WebSocket Proxy (internal, port $WS_INTERNAL_PORT, forward ke SSH 127.0.0.1:22)..."
WS_PORT="$WS_INTERNAL_PORT" WS_TARGET_HOST="127.0.0.1" WS_TARGET_PORT="22" \
    python3 /usr/local/bin/ws-proxy.py &

# --- Argo Tunnel (cloudflared) via Ingress Rules ---
if [ -n "$CF_TUNNEL_TOKEN" ] && [ -n "$CF_DOMAIN_WS" ] && [ -n "$CF_DOMAIN_SSL" ]; then
    echo "[*] Membuat berkas konfigurasi Ingress Rules untuk Cloudflare..."
    
    mkdir -p /etc/cloudflared
    
    # noTLSVerify dipaksa true dari dalam kontainer agar bypass sertifikat lokal murni
    cat <<EOF > /etc/cloudflared/config.yml
tunnel: $CF_TUNNEL_TOKEN
ingress:
  - hostname: $CF_DOMAIN_WS
    service: http://127.0.0.1:$WS_INTERNAL_PORT
  - hostname: $CF_DOMAIN_SSL
    service: https://127.0.0.1:$SSL_INTERNAL_PORT
    originRequest:
      originServerName: $CF_DOMAIN_SSL
      noTLSVerify: true
  - service: http_status:404
EOF

    echo "[*] Menjalankan Cloudflare Tunnel (Argo) via Ingress Config..."
    cloudflared tunnel --config /etc/cloudflared/config.yml run &
else
    echo "[!] Variabel Cloudflare belum lengkap (Butuh: CF_TUNNEL_TOKEN, CF_DOMAIN_WS, CF_DOMAIN_SSL) -> Argo Tunnel dilewati."
fi

echo "[*] Memulai Multiplexer di Port PUBLIK $PUBLIC_PORT (auto-deteksi SSL vs WS)..."
exec env \
    PORT="$PUBLIC_PORT" \
    SSL_TARGET_HOST="127.0.0.1" SSL_TARGET_PORT="$SSL_INTERNAL_PORT" \
    WS_MUX_TARGET_HOST="127.0.0.1" WS_MUX_TARGET_PORT="$WS_INTERNAL_PORT" \
    python3 /usr/local/bin/mux.py
