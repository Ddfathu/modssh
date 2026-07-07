#!/usr/bin/env python3
"""
WebSocket <-> SSH proxy (Dropbear Premium Matcher Version).

Menerima koneksi HTTP/WebSocket di suatu port. Script ini dimodifikasi khusus
agar sinkron dengan mekanisme jabat tangan Dropbear SSH yang menggunakan 
custom banner, serta menyaring sisa payload enhanced PATCH dengan aman.
Dituning khusus agar KEBAL terhadap payload super panjang (Anti 'too long line')
dan dioptimalkan dengan High-Speed Header Parsing untuk koneksi kilat.
"""

import asyncio
import base64
import hashlib
import logging
import os
import signal
import sys
import secrets

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("WS_PORT", "8880"))
TARGET_HOST = os.environ.get("WS_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("WS_TARGET_PORT", "22"))
DEFAULT_RESPONSE = os.environ.get(
    "WS_RESPONSE",
    "HTTP/1.1 101 Switching Protocols\r\n\r\n",
)

logging.basicConfig(
    level=logging.INFO,
    format="[ws-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ws-proxy")


def parse_headers(raw: bytes) -> dict:
    """Fungsi pembaca header kecepatan tinggi (High-Speed Engine).
    Hanya membaca bagian header HTTP, membuang sampah payload kosmetik di bawahnya
    sehingga proses jabat tangan menjadi instan dan hemat CPU."""
    headers = {}
    try:
        # Potong tepat pada batas akhir header (\r\n\r\n), abaikan body di bawahnya
        header_part = raw.split(b"\r\n\r\n", 1)[0]
        lines = header_part.decode(errors="ignore").split("\r\n")
        
        # Iterasi mulai dari baris kedua (baris pertama adalah Request Method/Path)
        for line in lines[1:]:
            if not line:
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                # .strip() membersihkan spasi liar hantu akibat manipulasi HTTP Custom
                headers[k.strip().lower()] = v.strip()
    except Exception as e:
        log.debug("Gagal parse header: %s", e)
    return headers


def make_accept_key(ws_key: str) -> str:
    sha1 = hashlib.sha1((ws_key + WS_MAGIC).encode()).digest()
    return base64.b64encode(sha1).decode()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    log.info("Koneksi masuk dari %s", peer)

    try:
        raw_headers = await reader.read(4096)
        if not raw_headers:
            writer.close()
            return

        headers = parse_headers(raw_headers)
        raw_text_lower = raw_headers.decode(errors="ignore").lower()

        # Cek tipe upgrade websocket secara presisi
        is_ws_upgrade = "upgrade: websocket" in raw_text_lower or headers.get("upgrade", "").lower() == "websocket"

        if is_ws_upgrade:
            ws_key = headers.get("sec-websocket-key")
            if not ws_key and "sec-websocket-key:" in raw_text_lower:
                try:
                    for line in raw_headers.decode(errors="ignore").split("\r\n"):
                        if "sec-websocket-key" in line.lower():
                            ws_key = line.split(":", 1)[1].strip()
                            break
                except Exception:
                    pass

            if not ws_key:
                log.info("Client tidak mengirim Sec-WebSocket-Key. Membuat key otomatis...")
                ws_key = base64.b64encode(secrets.token_bytes(16)).decode()

            accept_key = make_accept_key(ws_key)
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key}\r\n"
            )
            if "sec-websocket-protocol" in headers:
                response += f"Sec-WebSocket-Protocol: {headers['sec-websocket-protocol']}\r\n"
            response += "\r\n"
            writer.write(response.encode())
        else:
            writer.write(DEFAULT_RESPONSE.encode())

        await writer.drain()

        try:
            target_reader, target_writer = await asyncio.open_connection(
                TARGET_HOST, TARGET_PORT
            )
        except Exception as e:
            log.error("Gagal konek ke target %s:%s -> %s", TARGET_HOST, TARGET_PORT, e)
            writer.close()
            return

        # --- TUNING DROPBEAR FILTER: Memotong sisa teks HTTP PATCH tanpa merusak banner Dropbear ---
        async def pipe_client_to_ssh(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            first_packet = True
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    
                    if first_packet:
                        first_packet = False
                        # Jika paket pertama bocor membawa sampah HTTP mentah dari HTTP Custom
                        if b"PATCH" in data or b"HTTP/" in data:
                            # Bersihkan data kotor, cari apakah ada sisa data murni di belakangnya
                            if b"SSH" in data:
                                idx = data.find(b"SSH-")
                                data = data[idx:]
                            else:
                                log.info("Membuang sisa data kotor HTTP dari paket awal client")
                                continue
                    
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                pass
            except Exception as e:
                log.debug("pipe_client error: %s", e)
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        async def pipe_ssh_to_client(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                pass
            except Exception as e:
                log.debug("pipe_ssh error: %s", e)
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe_client_to_ssh(reader, target_writer),
            pipe_ssh_to_client(target_reader, writer),
        )

    except Exception as e:
        log.error("Error menangani klien %s: %s", peer, e)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        log.info("Koneksi %s ditutup", peer)


async def main():
    # 🔥 PENTING: Mengunci 'limit=8192' agar buffer kebal total dari payload super panjang
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT, limit=8192)
    log.info(
        "WS proxy jalan di %s:%s -> Dropbear Backend Active (High-Speed & Jumbo Payload Enabled)",
        LISTEN_HOST, LISTEN_PORT,
    )
    async with server:
        await server.serve_forever()


def handle_sigterm(*_):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
