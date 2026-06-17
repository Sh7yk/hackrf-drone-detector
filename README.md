<img width="1041" height="748" alt="image" src="https://github.com/user-attachments/assets/05041e27-3f28-4002-95d2-ae484ea7187c" />


# hackrf-drone-detector
The app is designed for drone signal detection, spectrum analysis, and experimental jamming using HackRF. False positives are possible, but the program attempts to analyze signals based on many drone-specific parameters.
## System Requirements
Operating System: 
- Linux (Ubuntu/Debian, Fedora, Arch, etc.) with Python 3.8+ support
Hardware:
- HackRF One for transmit/receive operation
- Antenna
Software Dependencies:
- Python3.8+ with pip
- hackrf-tools
Libraries:
- numpy, matplotlib, tkinter, pygame, requests, smtplib
___
## Installing System Dependencies
### Ubuntu / Debian
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-tk python3-dev
sudo apt install -y hackrf libhackrf-dev
sudo apt install -y portaudio19-dev
```
### Fedora
```bash
sudo dnf install -y python3 python3-pip python3-tkinter python3-devel
sudo dnf install -y hackrf hackrf-tools
```
### Arch Linux
```bash
sudo pacman -S python python-pip tk
sudo pacman -S hackrf
```
Make sure hackrf_transfer is in the PATH:
```bash
which hackrf_transfer
```
If the command is not found, check the installation of the hackrf-tools package.

## Download the programm
```bash
git clone https://github.com/Sh7yk/hackrf-drone-detector.git
cd hackrf-drone-detector
```
## Create venv(recomended)
```bash
python3 -m venv venv
source venv/bin/activate
```
## Installing Python dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
## Check that hackrf is detected by the system.
```bash
hackrf_info
```

## Start detect
```bash
python3 hackrf-drone-detector.py
```

<img width="1413" height="1160" alt="image" src="https://github.com/user-attachments/assets/7527da65-356d-44f5-985c-82a6b9593e07" />
