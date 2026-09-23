# college-wifi-autologin

Auto-login to a Cisco ISE captive portal on Ubuntu using Playwright.

## What it does

- Runs every 5 minutes via a systemd timer
- Also fires when NetworkManager detects you've joined the campus WiFi
- Skips work entirely if you already have internet
- Handles the 7-hour portal session expiry automatically

## Requirements

Ubuntu 22.04+, a user systemd session, NetworkManager.

## Install

```bash
git clone https://github.com/siddhant-jain-18/college-wifi-autologin.git
cd college-wifi-autologin
./install.sh