# Leafline

A small Python web app for moving your files to e-readers over your local network. Leafline implements the **Calibre wireless smart-device protocol** for compatible reader apps and also exposes an **OPDS 1.2 catalog** and ordinary browser downloads. It does not require Calibre to be installed.

Upload EPUB, CBZ, PDF, images, or any other file to your library; select a connected reader to send supported files wirelessly. Files and activity are stored locally and survive restarts.

## Quick start

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open **http://localhost:8000** on the computer running the server. Choose **Add files**, or drag files onto the page. The app shows the server's LAN address in **Devices**; use that address on the e-reader, not `localhost`.

Keep the terminal open while using the server. Press Ctrl+C to stop it. Your library is stored in `data/` by default; back up this directory while the server is stopped.

## Connect a reader

### Calibre wireless device

1. Connect the server computer and reader to the **same local Wi-Fi**. Guest networks, AP isolation, VPNs, and firewalls can prevent discovery or connections.
2. In a compatible reader app, start a **Calibre wireless-device** connection. For example, in KOReader open **Calibre → Connect**. Choose an inbox directory if prompted.
3. Leafline advertises itself using Calibre's UDP discovery and mDNS service. If it isn't discovered, enter the IP address and **wireless port** shown in Leafline's **Devices** page in the reader's custom Calibre address settings. The default wireless TCP port is **9090**.
4. The reader appears under **Devices**. In **Library**, use a file's menu to **Send to device**, or select several files and send them together. Track queued, sent, and failed transfers in **Activity → Transfers**.

The server keeps the original bytes; it does not convert formats. A wireless client decides which extensions it accepts and may reject other files. KOReader's default wireless extension list, for example, is not a list of every file Leafline can store. Use the browser or OPDS catalog for other files, if the device supports downloading them. A same-named file already in the reader's inbox may be replaced. Empty files are available through the web/OPDS download path but are not pushed wirelessly.

### OPDS or web browser

Add the catalog URL shown in **Devices** to an OPDS-compatible reading app. It looks like:

```text
http://<server-LAN-IP>:8000/opds
```

Or open `http://<server-LAN-IP>:8000/` in the device's browser to browse and download files. OPDS readers may hide formats they do not recognize, even though Leafline stores them. The download URLs include the original filename and support HTTP range requests.

### Compatibility and scope

Leafline implements discovery, initialization, device information, keepalives, and binary **send-to-device** commands from Calibre's smart-device protocol. It is designed for clients such as KOReader's Calibre wireless plugin and other compatible wireless-device apps. The protocol is tested against a simulated KOReader-style client; individual hardware and third-party apps have not all been verified. Some stock e-reader software has no Calibre wireless client; use a compatible app, OPDS client, or browser where available. No server can make every device understand a format or connection method it does not support.

This is **not** a full Calibre replacement: it does not import an existing Calibre database, convert books, remove DRM, synchronize collections or reading progress, browse files already on the device, or implement the entire Calibre Content Server API. EPUB title, author, and cover are read when available; other files use their filename until you edit their details.

## Configuration

```sh
python app.py --help
python app.py --port 8080 --wireless-port 9091 --data-dir /path/to/library
python app.py --advertise-host 192.168.1.42  # if automatic LAN IP selection is wrong
```

| Option or environment variable | Default | Purpose |
| --- | --- | --- |
| `--host` / `LEAFLINE_BIND` | `0.0.0.0` | Bind address for both listeners. Use `127.0.0.1` to restrict access to the same computer. |
| `--port` / `PORT` | `8000` | Web dashboard and OPDS TCP port. |
| `--wireless-port` / `LEAFLINE_WIRELESS_PORT` | `9090` | Calibre wireless TCP port. |
| `--data-dir` / `LEAFLINE_DATA_DIR` | `data` | Persistent files, covers, SQLite database, and transfer history. |
| `--advertise-host` / `LEAFLINE_ADVERTISE_HOST` | Auto-detected | LAN IPv4 address advertised to readers and shown in the UI. |
| `--no-mdns` | Off | Disable mDNS advertisements; UDP discovery and manual connections still work. |
| `LEAFLINE_HTTP_USER` | `leafline` | Username for optional web/OPDS HTTP Basic authentication. |
| `LEAFLINE_HTTP_PASSWORD` | Unset | Enable HTTP Basic authentication for the web dashboard and OPDS. |
| `LEAFLINE_WIRELESS_PASSWORD` | Unset | Require the Calibre wireless password on the reader. |

For automatic discovery, allow UDP ports **54982, 48123, 39001, 44044, 59678** and mDNS UDP **5353** on the local network, as well as TCP **9090** (wireless) and TCP **8000** (web/OPDS), or your configured ports. If some discovery ports are already in use by Calibre, Leafline uses those it can bind; manual connection still works. Stop another server or choose a different wireless TCP port if it cannot start. The selected address, listener state, and available discovery ports appear in Settings.

Example with optional passwords:

```sh
export LEAFLINE_HTTP_USER=reader
export LEAFLINE_HTTP_PASSWORD='choose-a-password'
export LEAFLINE_WIRELESS_PASSWORD='choose-another-password'
python app.py
```

**Security:** by default, anyone able to reach the server can read, upload, and delete library files. Passwords are optional compatibility/access controls, **not encryption**: HTTP Basic authentication, Calibre's wireless protocol, and file transfers are not encrypted. Run only on a trusted private network; do not expose the server or its ports directly to the public internet. The built-in Flask server is intended for personal/local use, not production deployment.

If running in a cloud workspace or preview, the shown IP belongs to that environment, not to your own Wi-Fi. Run Leafline on a computer reachable on the reader's LAN for direct wireless discovery and transfers.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests exercise file persistence, arbitrary binary downloads, EPUB metadata, OPDS, authentication, UDP discovery, mDNS registration, and a simulated Calibre wireless handshake and transfer. They do not replace testing with your particular physical reader.
