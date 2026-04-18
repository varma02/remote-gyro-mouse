> [!WARNING]  
> This is a very "vibe coded" project, intended for my personal use only. It may be buggy, insecure, and not suitable for production use.  
> **Use at your own risk.**

# Remote Gyro Mouse

Turn your phone into a gyro-based remote for controlling a media PC cursor over the local network.

## Features
- Gyro-based mouse movement
- Tap to click and drag to scroll
- WebSocket + HTTPS single-file UI

## Requirements
- Python 3
- A supported input backend:
  - **Wayland**: `python-evdev` (preferred)
  - **X11**: `xdotool`
  - Optional: `ydotool` for Wayland/X11

## Run
1. Generate TLS certs into `ssl/cert.pem` and `ssl/key.pem`.  
    Example command using OpenSSL:  
    `mkdir ssl && openssl req -x509 -newkey rsa:4096 -keyout ssl/key.pem -out ssl/cert.pem -days 365 -nodes -subj "/CN=localhost"`
2. Install dependencies:
   - Wayland: `pip install python-evdev`
   - X11: `sudo apt install xdotool`
3. Start the server:  
    `python3 main.py --host 0.0.0.0 --port 8443`
4. Open `https://<pc-ip>:8443` on your phone and enable gyro by tapping anywhere.  
   *Note: You may need to accept the self-signed cert warning and enable motion sensors in your browser.*
    

---

This project is under an [MIT License](license).
