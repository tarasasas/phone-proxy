# iPhone → PC proxy

Run a small proxy server on your iPhone and send your PC's traffic through it.
The traffic then leaves from the phone's cellular connection as the phone's own traffic.

```
 PC ──(hotspot Wi-Fi / USB)──► iPhone proxy :1080 ──(cellular)──► internet
```

There are two ways to run the proxy on the phone:

| | Native app (`ios/`) | Python script (`phone_proxy.py`) |
|---|---|---|
| Speed | Close to full cellular speed | Slower, because iSH emulates a PC processor |
| Setup | Built on GitHub, installed with Sideloadly | Install iSH, copy the script over |
| Upkeep | Re-install every 7 days with a free Apple ID | None |

| File | Runs on | What it does |
|---|---|---|
| `ios/` | iPhone | SwiftUI app with a SOCKS5 + HTTP proxy, traffic stats, and background keep-alive |
| `.github/workflows/build-ios.yml` | GitHub | Builds the app into an `.ipa` file on a free macOS runner |
| `phone_proxy.py` | iPhone (iSH) | The same proxy as a Python script. Uses only the standard library. |
| `pc/set-proxy.ps1` | Windows | Turns the Windows system proxy on or off and tests the connection. |

## 1a. Native app (recommended)

### Build it on GitHub
1. Create a GitHub repo and push this folder to it.
   A **public** repo gets free macOS build minutes. A private one has a limited monthly allowance, but each build only takes a few minutes.
2. The **Build iOS app** workflow runs on every push that changes `ios/`.
   You can also start it by hand: open **Actions → Build iOS app → Run workflow**.
3. When the run turns green, open it and download the **PhoneProxy-ipa** artifact.
   Unzip it to get `PhoneProxy.ipa`.

### Install it with Sideloadly (Windows)
1. Install **iTunes** and **iCloud** from Apple's website, not the Microsoft Store versions. Sideloadly needs their drivers.
   Then install [Sideloadly](https://sideloadly.io).
2. Plug in the iPhone and tap **Trust** on it.
3. Drag `PhoneProxy.ipa` into Sideloadly, enter your Apple ID, and click **Start**.
   A free Apple ID works. Sideloadly signs the app with it.
4. On the iPhone, open **Settings → Privacy & Security → Developer Mode** and turn it on. The phone restarts.
5. Open **Settings → General → VPN & Device Management**, tap your Apple ID, and tap **Trust**.
6. Open **Phone Proxy** and tap **Start proxy**. It shows the address for your PC, usually `172.20.10.1:1080`.

With a free Apple ID, the app stops opening after 7 days. To renew it, re-install it with Sideloadly; your settings are kept.
A paid developer account ($99/year) makes the app last a year.

### App settings
- **Keep alive: silent audio** (on by default) plays silence, so iOS doesn't pause the app in the background. No permission is needed.
  A phone call can interrupt it, and the app resumes afterwards.
- **Keep alive: location** is the most reliable option. It shows the blue location indicator while it runs.
  If the proxy stops when the screen locks, turn this on.
- **Only use cellular** makes outgoing traffic go over mobile data even when the phone is on Wi-Fi.
  With it on, the PC and the phone can both be on the same Wi-Fi network, and you don't need the hotspot.
- **Require login** sets a username and password for both SOCKS5 and the HTTP proxy.

After the app is running, continue at **step 2** below. The PC side is the same for the app and the script.

## 1b. Python script on the iPhone (iSH, free)

1. Install **iSH Shell** from the App Store. It's a small Linux shell for iOS.
2. In iSH, install Python:
   ```sh
   apk add python3
   ```
3. Copy `phone_proxy.py` to the phone. The easiest way:
   - Turn on **Personal Hotspot** on the iPhone and connect the PC to it.
   - On the PC, in this folder, run: `python -m http.server 8000`
   - Find the PC's IP with `ipconfig`. On a hotspot it looks like `172.20.10.x`.
   - In iSH, run: `wget http://172.20.10.x:8000/phone_proxy.py`
4. Keep iSH running in the background. iOS suspends apps otherwise.
   ```sh
   cat /dev/location > /dev/null &
   ```
   Give iSH location permission when asked ("Always" works best).
5. Start the proxy:
   ```sh
   python3 phone_proxy.py
   ```
   It prints the addresses to use. The hotspot address is usually `172.20.10.1`.

> Other iOS apps work too: **a-Shell** has Python built in, and **Pythonista** is paid.
> But iOS suspends them after a few minutes in the background. iSH's location trick is what keeps it alive.

## 2. Connect the PC

The PC has to be on the phone's network. Use one of these:
- **Wi-Fi:** join the iPhone's Personal Hotspot.
- **USB:** plug in the iPhone with Personal Hotspot on. This needs the Apple Devices / iTunes driver.

Then check that the proxy works:
```powershell
.\pc\set-proxy.ps1 -Test
```
It should print your phone's carrier IP.

## 3. Send traffic through it

Choose how much of the PC's traffic should use the proxy.

**Whole system (most apps):**
```powershell
.\pc\set-proxy.ps1            # on
.\pc\set-proxy.ps1 -Off       # off
```
This sets the Windows proxy setting, which uses the proxy's HTTP mode. Browsers, Windows Update, the Store and most apps follow this setting. Some games and apps that don't use HTTP ignore it.

**Firefox only:** Settings → Network Settings → Manual proxy.
Set SOCKS Host to `172.20.10.1`, port `1080`, and choose SOCKS v5.
Also tick **Proxy DNS when using SOCKS v5**.

**Chrome/Edge only:**
```
chrome.exe --proxy-server="socks5://172.20.10.1:1080"
```

**Everything, including games and UDP:** use a tool that captures all traffic and sends it to the SOCKS5 proxy, such as Proxifier or a tun2socks client like sing-box or v2rayN.
Point it at `172.20.10.1:1080` (SOCKS5).

## Options

```sh
python3 phone_proxy.py --port 8888                   # different port
python3 phone_proxy.py --user me --password secret   # require a login
python3 phone_proxy.py --stats 0                     # no traffic reports
```
`set-proxy.ps1 -Test` doesn't send a login. If you set one, test with curl instead:
`curl.exe --proxy socks5h://me:secret@172.20.10.1:1080 https://api.ipify.org`.
Windows' system proxy prompts for the login.

## Limitations

- **Speed:** iSH emulates an x86 CPU, so expect lower speeds than a direct hotspot connection.
  Browsing and streaming work fine. Very large downloads will hit a CPU limit on the phone.
- **TCP only:** SOCKS5 `CONNECT` is supported, UDP isn't. So QUIC and HTTP/3 fall back to TCP, which works fine, and most online-game UDP won't go through.
- **Carrier rules:** some plans don't allow tethering. Check yours if you're using this to avoid hotspot limits.
