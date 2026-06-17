import numpy as np
import time
import subprocess
import re
import threading
from datetime import datetime
from collections import deque
import sys
import smtplib
from email.mime.text import MIMEText
import requests
import json
import os

import tkinter as tk
from tkinter import ttk, messagebox, Scale
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.animation import FuncAnimation
import matplotlib.gridspec as gridspec

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

# --------------------- Constants ---------------------
DEFAULT_FREQUENCIES = [433e6, 868e6, 915e6, 2400e6, 5800e6]
DEFAULT_BANDWIDTHS = {
    433e6: 8e6,
    868e6: 8e6,
    915e6: 8e6,
    2400e6: 8e6,
    5800e6: 20e6,
}
DEFAULT_THRESHOLDS = {433e6: -25, 868e6: -22, 915e6: -20, 2400e6: -18, 5800e6: -20}
HISTORY_LEN = 200
PLOT_WINDOW = 60

# --------------------- Configuration Manager ---------------------
class ConfigManager:
    CONFIG_FILE = "config.json"

    @classmethod
    def load(cls):
        if not os.path.exists(cls.CONFIG_FILE):
            return None
        try:
            with open(cls.CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    @classmethod
    def save(cls, config):
        try:
            with open(cls.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save settings: {e}")

# --------------------- Core Detector ---------------------
class DroneCore:
    def __init__(self, frequencies=None, thresholds=None, bandwidths=None):
        self.frequencies = list(frequencies or DEFAULT_FREQUENCIES)
        self.freq_thresholds = dict(thresholds or DEFAULT_THRESHOLDS)
        if bandwidths is None:
            self.bandwidths = {}
            for f in self.frequencies:
                self.bandwidths[f] = DEFAULT_BANDWIDTHS.get(f, 8e6)
        else:
            self.bandwidths = dict(bandwidths)
            for f in self.frequencies:
                if f not in self.bandwidths:
                    self.bandwidths[f] = DEFAULT_BANDWIDTHS.get(f, 8e6)

        for f in self.frequencies:
            if f not in self.freq_thresholds:
                self.freq_thresholds[f] = -20

        self.history = {f: deque(maxlen=HISTORY_LEN) for f in self.frequencies}
        self.detection_counts = {f: 0 for f in self.frequencies}

        self.adaptive_threshold_enabled = False
        self.adaptive_offset = 5.0
        self.noise_history = {f: deque(maxlen=100) for f in self.frequencies}
        for f in self.frequencies:
            self.noise_history[f].extend([-65] * 50)

        self.std_window = 20
        self.std_threshold = 3.0
        self.autocorr_threshold = 0.6
        self.crossings_threshold = 5
        self.trend_sensitivity = 0.3
        self.trend_window = 15

        self.scan_mode = 'normal'
        self.tracking_timeout = 10.0

        self.email_enabled = False
        self.email_from = ''
        self.email_password = ''
        self.email_to = ''
        self.email_smtp = 'smtp.gmail.com:587'
        self.telegram_enabled = False
        self.telegram_token = ''
        self.telegram_chat_id = ''

        self.tracking_mode = False
        self.tracked_frequency = None
        self.track_start_time = 0
        self.track_rssi_history = deque(maxlen=100)
        self.detection_cooldown = 3.0
        self.last_alert = 0
        self._last_track_check = 0

        self._init_sound()
        self.sound_volume = 0.5

        # ---- Jamming ----
        self.jamming_enabled = False
        self.jamming_power = 10
        self.jamming_duration = 2.0
        self.jamming_check_interval = 0.5
        self.jamming_active = False
        self.jamming_start_time = 0
        self.jamming_prev_rssi = None
        self.jamming_process = None
        self.noise_file_path = "noise.iq"
        self._noise_generated = False
        self._current_noise_sr = None

    # ---------- Sound ----------
    def _init_sound(self):
        self.sound_enabled = False
        self.alert_sound = None
        self.tracking_sound = None
        if not PYGAME_AVAILABLE:
            return
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=1024)
            self.sound_enabled = True
            self._build_alert_sound()
            self._build_tracking_sound()
        except Exception:
            self.sound_enabled = False

    def _build_alert_sound(self):
        try:
            sr = 44100
            t = np.linspace(0, 0.6, int(sr * 0.6))
            tone = 0.5 * np.sin(2 * np.pi * 1200 * t)
            audio = np.int16(tone * 32767)
            self.alert_sound = pygame.sndarray.make_sound(audio)
            self.alert_sound.set_volume(self.sound_volume)
        except Exception:
            self.alert_sound = None

    def _build_tracking_sound(self, freq_hz=800):
        try:
            sr = 44100
            dur = 0.08
            t = np.linspace(0, dur, int(sr * dur))
            tone = 0.3 * np.sin(2 * np.pi * freq_hz * t)
            env = np.exp(-5 * t)
            audio = np.int16(tone * env * 32767)
            self.tracking_sound = pygame.sndarray.make_sound(audio)
            self.tracking_sound.set_volume(self.sound_volume)
        except Exception:
            self.tracking_sound = None

    def set_volume(self, vol):
        self.sound_volume = max(0.0, min(1.0, vol))
        if self.alert_sound:
            self.alert_sound.set_volume(self.sound_volume)
        if self.tracking_sound:
            self.tracking_sound.set_volume(self.sound_volume)

    def play_alert(self):
        if self.sound_enabled and self.alert_sound:
            if time.time() - self.last_alert > self.detection_cooldown:
                self.alert_sound.play()

    def play_tracking_beep(self, rssi):
        if not (self.sound_enabled and self.tracking_sound):
            return
        freq = self.tracked_frequency
        thr = self.freq_thresholds.get(freq, -20)
        min_rssi, max_rssi = thr - 5, -5
        frac = (max(min_rssi, min(rssi, max_rssi)) - min_rssi) / (max_rssi - min_rssi + 1e-9)
        interval = 1.5 - frac * (1.5 - 0.1)
        now = time.time()
        if not hasattr(self, '_last_beep'):
            self._last_beep = 0
        if now - self._last_beep >= interval:
            self.tracking_sound.play()
            self._last_beep = now

    # ---------- HackRF ----------
    @staticmethod
    def check_hackrf():
        try:
            r = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=3)
            return "Board ID" in r.stdout
        except Exception:
            return False

    _last_freq = None
    _last_rssi = None
    _last_time = 0
    CACHE_DELTA = 1e6

    def get_rssi(self, freq):
        now = time.time()
        if (self._last_freq is not None and abs(freq - self._last_freq) < self.CACHE_DELTA
                and now - self._last_time < 0.05):
            return self._last_rssi

        sample_rate = int(self.bandwidths.get(freq, 8e6))
        try:
            cmd = [
                "hackrf_transfer", "-r", "/dev/null",
                "-f", str(int(freq)),
                "-s", str(sample_rate),
                "-n", "200000",
                "-a", "0",
                "-l", "24",
                "-g", "32",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5)
            out = r.stderr + "\n" + r.stdout
            m = re.search(r'average power\s+(-?\d+\.?\d*)\s*dBfs', out, re.IGNORECASE)
            if m:
                rssi = float(m.group(1))
            else:
                matches = re.findall(r'(-?\d+\.?\d*)\s*dB', out)
                if matches:
                    rssi = max(float(v) for v in matches)
                else:
                    rssi = -65.0
            self._last_freq = freq
            self._last_rssi = rssi
            self._last_time = now
            return rssi
        except Exception:
            return -65.0

    # ---------- Noise file generation for jamming ----------
    def _generate_noise_file(self, sample_rate):
        """Creates noise.iq file with 2 seconds of white noise (int16 I/Q)."""
        if self._noise_generated and self._current_noise_sr == sample_rate:
            expected_size = sample_rate * 2 * 2 * 2  # 2 sec * 2 bytes * 2 (I/Q)
            if os.path.exists(self.noise_file_path) and os.path.getsize(self.noise_file_path) == expected_size:
                return
        try:
            duration = 2  # seconds
            num_samples = int(sample_rate * duration)
            noise_i = np.random.randint(-32767, 32767, size=num_samples, dtype=np.int16)
            noise_q = np.random.randint(-32767, 32767, size=num_samples, dtype=np.int16)
            noise = np.empty((num_samples * 2,), dtype=np.int16)
            noise[0::2] = noise_i
            noise[1::2] = noise_q
            noise.tofile(self.noise_file_path)
            self._noise_generated = True
            self._current_noise_sr = sample_rate
            print(f"[JAM] Noise file created: {self.noise_file_path}, size {os.path.getsize(self.noise_file_path)} bytes")
        except Exception as e:
            print(f"[JAM] Noise generation error: {e}")

    def start_jamming(self, freq):
        """Start transmitting noise on the given frequency."""
        self.stop_jamming()
        sample_rate = int(self.bandwidths.get(freq, 8e6))
        self._generate_noise_file(sample_rate)
        try:
            cmd = [
                "hackrf_transfer", "-t", self.noise_file_path,
                "-R",
                "-f", str(int(freq)),
                "-s", str(sample_rate),
                "-a", "1",
                "-g", str(self.jamming_power),
            ]
            print(f"[JAM] Starting: {' '.join(cmd)}")
            self.jamming_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.5)
            if self.jamming_process.poll() is not None:
                stdout, stderr = self.jamming_process.communicate()
                print(f"[JAM] Process exited immediately. stderr: {stderr.decode()}")
                self.jamming_process = None
                return False
            self.jamming_active = True
            self.jamming_start_time = time.time()
            print(f"[JAM] Transmission started at {freq/1e6:.3f} MHz, gain {self.jamming_power} dB")
            return True
        except Exception as e:
            print(f"[JAM] Jamming start error: {e}")
            self.jamming_process = None
            return False

    def stop_jamming(self):
        """Stop transmission."""
        if self.jamming_process is not None:
            print("[JAM] Stopping transmission...")
            self.jamming_process.terminate()
            try:
                self.jamming_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.jamming_process.kill()
                self.jamming_process.wait()
            self.jamming_process = None
            self.jamming_active = False
            print("[JAM] Transmission stopped.")

    # ---------- Signal Analysis ----------
    def analyze_signal_signature(self, freq):
        hist = list(self.history[freq])
        if len(hist) < self.std_window:
            return None

        std_dev = np.std(hist[-self.std_window:])

        autocorr = 0.0
        if len(hist) > 30:
            try:
                autocorr = float(np.corrcoef(hist[-30:-10], hist[-20:])[0, 1])
            except Exception:
                autocorr = 0.0

        if len(hist) >= 30:
            recent = hist[-30:]
            mean_val = np.mean(recent)
            crossings = sum(1 for i in range(1, len(recent))
                           if (recent[i-1] - mean_val) * (recent[i] - mean_val) < 0)
        else:
            mean_val = np.mean(hist)
            crossings = sum(1 for i in range(1, len(hist))
                           if (hist[i-1] - mean_val) * (hist[i] - mean_val) < 0)

        if len(hist) >= 20:
            deriv = np.diff(hist[-20:])
            pulsation = np.std(deriv)
        else:
            pulsation = 0.0
        has_pulsations = pulsation > 1.0

        is_drone_like = (std_dev > self.std_threshold and
                         autocorr < self.autocorr_threshold and
                         crossings > self.crossings_threshold and
                         has_pulsations)

        return {
            "std_dev": std_dev,
            "autocorr": autocorr,
            "crossings": crossings,
            "pulsation": pulsation,
            "has_pulsations": has_pulsations,
            "is_drone_like": is_drone_like,
        }

    def analyze_movement(self, freq, window=None):
        if window is None:
            window = self.trend_window
        hist = list(self.history[freq])
        if len(hist) < window:
            return None
        alpha = 0.3
        smoothed, last = [], hist[0]
        for v in hist:
            last = alpha * v + (1 - alpha) * last
            smoothed.append(last)
        recent = smoothed[-window:]
        trend = float(np.polyfit(range(len(recent)), recent, 1)[0])
        acc = float(np.gradient(np.gradient(recent)).mean()) if len(recent) > 10 else 0.0
        if trend > self.trend_sensitivity:
            speed = "Approaching"
        elif trend < -self.trend_sensitivity:
            speed = "Receding"
        else:
            speed = "Stable"
        return {"trend": trend, "acceleration": acc, "speed": speed}

    # ---------- Notifications ----------
    def send_email_alert(self, freq, rssi):
        if not self.email_enabled:
            return
        try:
            msg = MIMEText(f"Signal detected at {freq/1e6:.3f} MHz\nRSSI: {rssi:.1f} dB\nTime: {datetime.now()}")
            msg['Subject'] = '🚁 Drone Detector: Alert!'
            msg['From'] = self.email_from
            msg['To'] = self.email_to
            s = smtplib.SMTP(self.email_smtp.split(':')[0], int(self.email_smtp.split(':')[1]))
            s.starttls()
            s.login(self.email_from, self.email_password)
            s.send_message(msg)
            s.quit()
        except Exception as e:
            print(f"Email error: {e}")

    def send_telegram_alert(self, freq, rssi):
        if not self.telegram_enabled:
            return
        try:
            text = (f"🚁 Drone Detector!\n"
                    f"Freq: {freq/1e6:.3f} MHz\n"
                    f"RSSI: {rssi:.1f} dB\n"
                    f"Time: {datetime.now().strftime('%H:%M:%S')}")
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "HTML"}
            requests.post(url, data=data, timeout=5)
        except Exception as e:
            print(f"Telegram error: {e}")

    # ---------- Scanning ----------
    def scan_once(self):
        results = {}
        for freq in self.frequencies:
            rssi = self.get_rssi(freq)
            self.history[freq].append(rssi)

            if self.adaptive_threshold_enabled:
                noise_median = np.median(self.noise_history[freq])
                thr = noise_median + self.adaptive_offset
                self.freq_thresholds[freq] = thr
            else:
                thr = self.freq_thresholds.get(freq, -20)

            if self.adaptive_threshold_enabled:
                self.noise_history[freq].append(rssi)

            sig = self.analyze_signal_signature(freq)
            mov = self.analyze_movement(freq)
            is_signal = rssi > thr
            is_approaching = mov and mov["trend"] > self.trend_sensitivity
            is_drone_like = sig and sig["is_drone_like"]
            alert = is_signal and (is_approaching or is_drone_like)

            results[freq] = {
                "rssi": rssi,
                "threshold": thr,
                "signature": sig,
                "movement": mov,
                "alert": alert,
                "confidence": "HIGH" if (sig and sig["is_drone_like"]) else "MEDIUM",
            }
        return results

    def scan_tracking(self):
        freq = self.tracked_frequency
        thr = self.freq_thresholds.get(freq, -20)

        # ---------- Jamming mode ----------
        if self.jamming_enabled and self.tracking_mode:
            if not self.jamming_active:
                rssi = self.get_rssi(freq)
                self.history[freq].append(rssi)
                self.track_rssi_history.append(rssi)
                mov = self.analyze_movement(freq)
                if rssi > thr + 5:
                    if self.start_jamming(freq):
                        self.jamming_prev_rssi = rssi
                        return {"rssi": rssi, "movement": mov, "lost": False, "freq": freq, "jamming": True}
                    else:
                        pass
                lost = rssi < thr - 10
                if self.scan_mode == 'tracking_timeout':
                    if time.time() - self.track_start_time > self.tracking_timeout:
                        lost = True
                self.play_tracking_beep(rssi)
                return {"rssi": rssi, "movement": mov, "lost": lost, "freq": freq, "jamming": False}
            else:
                if time.time() - self.jamming_start_time >= self.jamming_duration:
                    self.stop_jamming()
                    rssi_after = self.get_rssi(freq)
                    self.history[freq].append(rssi_after)
                    self.track_rssi_history.append(rssi_after)
                    mov = self.analyze_movement(freq)
                    if self.jamming_prev_rssi is not None and rssi_after < self.jamming_prev_rssi - 2:
                        self.tracking_mode = False
                        self.tracked_frequency = None
                        self.track_rssi_history.clear()
                        self.jamming_prev_rssi = None
                        return {"rssi": rssi_after, "movement": mov, "lost": True, "freq": freq, "jamming": False}
                    else:
                        self.start_jamming(freq)
                        self.jamming_prev_rssi = rssi_after
                        return {"rssi": rssi_after, "movement": mov, "lost": False, "freq": freq, "jamming": True}
                else:
                    return {"rssi": self.jamming_prev_rssi, "movement": None, "lost": False,
                            "freq": freq, "jamming": True}

        # ---------- Normal tracking (no jamming) ----------
        rssi = self.get_rssi(freq)
        self.history[freq].append(rssi)
        self.track_rssi_history.append(rssi)
        mov = self.analyze_movement(freq)
        lost = rssi < thr - 10
        if self.scan_mode == 'tracking_timeout':
            if time.time() - self.track_start_time > self.tracking_timeout:
                lost = True
        self.play_tracking_beep(rssi)
        return {"rssi": rssi, "movement": mov, "lost": lost, "freq": freq, "jamming": False}

    def __del__(self):
        self.stop_jamming()


# --------------------- GUI ---------------------
class DroneDetectorGUI:
    SCAN_INTERVAL_MS = 100
    LOG_MAX = 500

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Drone Detector v6.0")
        self.root.minsize(1200, 750)

        self.core: DroneCore | None = None
        self.running = False
        self._scan_thread = None
        self._pending = None
        self._lock = threading.Lock()

        self._freq_vars = []
        self._thr_vars = {}
        self._bw_vars = {}
        self._freq_enabled = []

        self._analysis_vars = {}
        self._email_vars = {}
        self._telegram_vars = {}
        self._adaptive_enabled_var = tk.BooleanVar(value=False)
        self._adaptive_offset_var = tk.StringVar(value="5.0")
        self._tracking_timeout_var = tk.StringVar(value="10.0")
        self._cooldown_var = tk.StringVar(value="3.0")
        self._email_enabled_var = tk.BooleanVar(value=False)
        self._telegram_enabled_var = tk.BooleanVar(value=False)
        self._volume_value = tk.DoubleVar(value=0.5)

        self._jamming_enabled_var = tk.BooleanVar(value=False)
        self._jamming_power_var = tk.StringVar(value="10")
        self._jamming_duration_var = tk.StringVar(value="2.0")
        self._jamming_check_interval_var = tk.StringVar(value="0.5")

        self._freq_list = list(DEFAULT_FREQUENCIES)
        self._thr_dict = dict(DEFAULT_THRESHOLDS)
        self._bw_dict = dict(DEFAULT_BANDWIDTHS)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._load_settings()

    # ---------- Load/Save Settings ----------
    def _load_settings(self):
        config = ConfigManager.load()
        if config is None:
            self._init_default_freqs()
            return
        try:
            freqs = config.get("frequencies", DEFAULT_FREQUENCIES)
            thrs = config.get("thresholds", DEFAULT_THRESHOLDS)
            bws = config.get("bandwidths", DEFAULT_BANDWIDTHS)
            self._freq_vars.clear()
            self._thr_vars.clear()
            self._bw_vars.clear()
            self._freq_enabled.clear()
            for f in freqs:
                idx = len(self._freq_vars)
                self._freq_vars.append(tk.StringVar(value=str(int(f))))
                self._freq_enabled.append(tk.BooleanVar(value=True))
                self._thr_vars[idx] = tk.StringVar(value=str(thrs.get(f, -20)))
                bw_mhz = bws.get(f, DEFAULT_BANDWIDTHS.get(f, 8e6)) / 1e6
                self._bw_vars[idx] = tk.StringVar(value=f"{bw_mhz:.1f}")
            for key in ["std_window", "std_threshold", "autocorr_threshold",
                        "crossings_threshold", "trend_sensitivity", "trend_window"]:
                if key in config:
                    self._analysis_vars[key].set(str(config[key]))
            self._adaptive_enabled_var.set(config.get("adaptive_enabled", False))
            self._adaptive_offset_var.set(str(config.get("adaptive_offset", 5.0)))
            self._mode_combo.set(config.get("scan_mode", "normal"))
            self._tracking_timeout_var.set(str(config.get("tracking_timeout", 10.0)))
            self._cooldown_var.set(str(config.get("cooldown", 3.0)))
            self._email_enabled_var.set(config.get("email_enabled", False))
            for key in ["email_from", "email_password", "email_to", "email_smtp"]:
                if key in config:
                    self._email_vars[key].set(config[key])
            self._telegram_enabled_var.set(config.get("telegram_enabled", False))
            for key in ["telegram_token", "telegram_chat_id"]:
                if key in config:
                    self._telegram_vars[key].set(config[key])
            self._volume_value.set(config.get("volume", 0.5))
            self._jamming_enabled_var.set(config.get("jamming_enabled", False))
            self._jamming_power_var.set(str(config.get("jamming_power", 10)))
            self._jamming_duration_var.set(str(config.get("jamming_duration", 2.0)))
            self._jamming_check_interval_var.set(str(config.get("jamming_check_interval", 0.5)))
            self._refresh_settings_tab()
            if self.core is None:
                self._apply_settings(silent=True)
            else:
                self._apply_settings_to_core()
        except Exception as e:
            print(f"Settings load error: {e}")
            self._init_default_freqs()

    def _init_default_freqs(self):
        self._freq_vars.clear()
        self._thr_vars.clear()
        self._bw_vars.clear()
        self._freq_enabled.clear()
        for f in DEFAULT_FREQUENCIES:
            idx = len(self._freq_vars)
            self._freq_vars.append(tk.StringVar(value=str(int(f))))
            self._freq_enabled.append(tk.BooleanVar(value=True))
            self._thr_vars[idx] = tk.StringVar(value=str(DEFAULT_THRESHOLDS.get(f, -20)))
            bw_mhz = DEFAULT_BANDWIDTHS.get(f, 8e6) / 1e6
            self._bw_vars[idx] = tk.StringVar(value=f"{bw_mhz:.1f}")
        self._refresh_settings_tab()

    def _save_settings(self):
        config = {}
        freqs, thrs, bws = [], {}, {}
        for idx, fvar in enumerate(self._freq_vars):
            if not self._freq_enabled[idx].get():
                continue
            try:
                f = float(fvar.get())
                t = float(self._thr_vars[idx].get())
                bw_mhz = float(self._bw_vars[idx].get())
            except ValueError:
                continue
            freqs.append(f)
            thrs[f] = t
            bws[f] = bw_mhz * 1e6
        config["frequencies"] = freqs
        config["thresholds"] = thrs
        config["bandwidths"] = bws
        for key in self._analysis_vars:
            config[key] = self._analysis_vars[key].get()
        config["adaptive_enabled"] = self._adaptive_enabled_var.get()
        config["adaptive_offset"] = self._adaptive_offset_var.get()
        config["scan_mode"] = self._mode_combo.get()
        config["tracking_timeout"] = self._tracking_timeout_var.get()
        config["cooldown"] = self._cooldown_var.get()
        config["email_enabled"] = self._email_enabled_var.get()
        for key in self._email_vars:
            config[key] = self._email_vars[key].get()
        config["telegram_enabled"] = self._telegram_enabled_var.get()
        for key in self._telegram_vars:
            config[key] = self._telegram_vars[key].get()
        config["volume"] = self._volume_value.get()
        config["jamming_enabled"] = self._jamming_enabled_var.get()
        config["jamming_power"] = self._jamming_power_var.get()
        config["jamming_duration"] = self._jamming_duration_var.get()
        config["jamming_check_interval"] = self._jamming_check_interval_var.get()
        ConfigManager.save(config)

    def _apply_settings_to_core(self):
        if self.core is None:
            return
        freqs, thrs, bws = [], {}, {}
        for idx, fvar in enumerate(self._freq_vars):
            if not self._freq_enabled[idx].get():
                continue
            try:
                f = float(fvar.get())
                t = float(self._thr_vars[idx].get())
                bw_mhz = float(self._bw_vars[idx].get())
            except ValueError:
                continue
            freqs.append(f)
            thrs[f] = t
            bws[f] = bw_mhz * 1e6
        self.core.frequencies = freqs
        self.core.freq_thresholds = thrs
        self.core.bandwidths = bws
        try:
            self.core.std_window = int(self._analysis_vars["std_window"].get())
            self.core.std_threshold = float(self._analysis_vars["std_threshold"].get())
            self.core.autocorr_threshold = float(self._analysis_vars["autocorr_threshold"].get())
            self.core.crossings_threshold = float(self._analysis_vars["crossings_threshold"].get())
            self.core.trend_sensitivity = float(self._analysis_vars["trend_sensitivity"].get())
            self.core.trend_window = int(self._analysis_vars["trend_window"].get())
        except ValueError:
            pass
        self.core.adaptive_threshold_enabled = self._adaptive_enabled_var.get()
        try:
            self.core.adaptive_offset = float(self._adaptive_offset_var.get())
        except ValueError:
            pass
        self.core.scan_mode = self._mode_combo.get()
        try:
            self.core.tracking_timeout = float(self._tracking_timeout_var.get())
        except ValueError:
            pass
        try:
            self.core.detection_cooldown = float(self._cooldown_var.get())
        except ValueError:
            pass
        self.core.email_enabled = self._email_enabled_var.get()
        self.core.email_from = self._email_vars["email_from"].get()
        self.core.email_password = self._email_vars["email_password"].get()
        self.core.email_to = self._email_vars["email_to"].get()
        self.core.email_smtp = self._email_vars["email_smtp"].get()
        self.core.telegram_enabled = self._telegram_enabled_var.get()
        self.core.telegram_token = self._telegram_vars["telegram_token"].get()
        self.core.telegram_chat_id = self._telegram_vars["telegram_chat_id"].get()
        self.core.set_volume(self._volume_value.get())
        self.core.jamming_enabled = self._jamming_enabled_var.get()
        try:
            self.core.jamming_power = int(self._jamming_power_var.get())
        except ValueError:
            pass
        try:
            self.core.jamming_duration = float(self._jamming_duration_var.get())
        except ValueError:
            pass
        try:
            self.core.jamming_check_interval = float(self._jamming_check_interval_var.get())
        except ValueError:
            pass
        self._rebuild_plots(freqs, thrs)
        self._rebuild_tree(freqs)
        self._log_append("Settings applied to core.", tag="info")

    # ---------- Build UI ----------
    def _build_ui(self):
        top = tk.Frame(self.root, pady=6, padx=10)
        top.pack(fill=tk.X)

        self._btn_start = tk.Button(top, text="▶ Start", width=12,
                                    command=self._on_start, bg="#2d6a4f", fg="white",
                                    relief=tk.FLAT, font=("Arial", 10, "bold"))
        self._btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_stop = tk.Button(top, text="■ Stop", width=12,
                                   command=self._on_stop, bg="#c0392b", fg="white",
                                   relief=tk.FLAT, font=("Arial", 10, "bold"),
                                   state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=(0, 12))

        self._status_var = tk.StringVar(value="Stopped")
        tk.Label(top, textvariable=self._status_var, font=("Arial", 11),
                 fg="#555").pack(side=tk.LEFT)

        self._mode_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self._mode_var, font=("Arial", 11, "bold"),
                 fg="#c0392b").pack(side=tk.LEFT, padx=20)

        tk.Button(top, text="Exit tracking", command=self._exit_tracking,
                  relief=tk.FLAT).pack(side=tk.RIGHT)

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        monitor_frame = tk.Frame(nb)
        nb.add(monitor_frame, text="  Monitoring  ")
        self._build_monitor_tab(monitor_frame)

        settings_frame = tk.Frame(nb)
        nb.add(settings_frame, text="  Settings  ")
        self._settings_container = settings_frame
        self._init_settings_vars()
        self._build_settings_tab(settings_frame)

        stats_frame = tk.Frame(nb)
        nb.add(stats_frame, text="  Statistics  ")
        self._build_stats_tab(stats_frame)

    def _init_settings_vars(self):
        params = [
            ("std_window", "STD window (points)", "20"),
            ("std_threshold", "STD threshold (dB)", "3.0"),
            ("autocorr_threshold", "Max autocorrelation", "0.6"),
            ("crossings_threshold", "Min zero crossings", "5"),
            ("trend_sensitivity", "Movement sensitivity (dB/step)", "0.3"),
            ("trend_window", "Trend window (points)", "15"),
        ]
        for name, _, default in params:
            self._analysis_vars[name] = tk.StringVar(value=default)

        email_fields = [
            ("email_from", ""),
            ("email_password", ""),
            ("email_to", ""),
            ("email_smtp", "smtp.gmail.com:587"),
        ]
        for key, default in email_fields:
            self._email_vars[key] = tk.StringVar(value=default)

        tg_fields = [
            ("telegram_token", ""),
            ("telegram_chat_id", ""),
        ]
        for key, default in tg_fields:
            self._telegram_vars[key] = tk.StringVar(value=default)

    # ---------- Monitoring Tab ----------
    def _build_monitor_tab(self, parent):
        paned = tk.PanedWindow(parent, orient=tk.HORIZONTAL, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(paned, width=270)
        paned.add(left, minsize=230)

        tk.Label(left, text="Frequency Status", font=("Arial", 10, "bold"),
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(8, 4))

        cols = ("freq", "rssi", "threshold", "status")
        self._tree = ttk.Treeview(left, columns=cols, show="headings", height=10)
        self._tree.heading("freq", text="MHz")
        self._tree.heading("rssi", text="RSSI, dB")
        self._tree.heading("threshold", text="Threshold")
        self._tree.heading("status", text="Status")
        self._tree.column("freq", width=70, anchor=tk.CENTER)
        self._tree.column("rssi", width=70, anchor=tk.CENTER)
        self._tree.column("threshold", width=60, anchor=tk.CENTER)
        self._tree.column("status", width=90, anchor=tk.CENTER)
        self._tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._tree.tag_configure("alert", background="#fdecea")
        self._tree.tag_configure("tracking", background="#e8f5e9")
        self._tree.tag_configure("jamming", background="#fff3cd")
        self._tree.tag_configure("ok", background="")

        tk.Label(left, text="Event Log", font=("Arial", 10, "bold"),
                 anchor=tk.W).pack(fill=tk.X, padx=8, pady=(8, 2))
        log_frame = tk.Frame(left)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._log = tk.Text(log_frame, height=10, font=("Courier", 9),
                            state=tk.DISABLED, wrap=tk.WORD)
        sb = tk.Scrollbar(log_frame, command=self._log.yview)
        self._log.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._log.tag_config("alert", foreground="#c0392b")
        self._log.tag_config("track", foreground="#2980b9")
        self._log.tag_config("jamming", foreground="#d4a017")
        self._log.tag_config("info", foreground="#555")

        right = tk.Frame(paned)
        paned.add(right)
        self._build_plots(right)

    def _build_plots(self, parent):
        self._fig = plt.Figure(figsize=(8, 5), tight_layout=True)
        self._axes = []
        self._lines = []
        self._threshold_lines = []
        self._threshold_artists = []

        n = len(DEFAULT_FREQUENCIES)
        gs = gridspec.GridSpec(n, 1, figure=self._fig, hspace=0.55)
        colors = ["#1a78c2", "#16a085", "#c0392b", "#8e44ad", "#e67e22"]

        for i, freq in enumerate(DEFAULT_FREQUENCIES):
            ax = self._fig.add_subplot(gs[i])
            ax.set_title(f"{freq/1e6:.1f} MHz", fontsize=8, loc="left", pad=2)
            ax.set_ylim(-80, 0)
            ax.set_xlim(0, PLOT_WINDOW)
            ax.set_ylabel("dB", fontsize=7)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3, linewidth=0.5)
            color = colors[i % len(colors)]
            (line,) = ax.plot([], [], color=color, linewidth=1.2)
            thr_line = ax.axhline(
                y=DEFAULT_THRESHOLDS.get(freq, -20),
                color="#e74c3c", linewidth=0.8, linestyle="--", alpha=0.7,
                picker=5
            )
            self._axes.append(ax)
            self._lines.append(line)
            self._threshold_lines.append(thr_line)
            self._threshold_artists.append(thr_line)

        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self._fig.canvas.mpl_connect('pick_event', self._on_threshold_pick)
        self._fig.canvas.mpl_connect('motion_notify_event', self._on_threshold_drag)
        self._fig.canvas.mpl_connect('button_release_event', self._on_threshold_release)
        self._drag_data = {'line': None, 'freq': None, 'y0': None}

    # ---------- Interactive Thresholds ----------
    def _on_threshold_pick(self, event):
        if event.artist in self._threshold_artists:
            self._drag_data['line'] = event.artist
            self._drag_data['y0'] = event.mouseevent.ydata
            idx = self._threshold_artists.index(event.artist)
            if self.core and idx < len(self.core.frequencies):
                self._drag_data['freq'] = self.core.frequencies[idx]

    def _on_threshold_drag(self, event):
        if self._drag_data['line'] is None or event.inaxes is None:
            return
        if event.button == 1:
            new_y = event.ydata
            if new_y > 0:
                new_y = -1
            elif new_y < -80:
                new_y = -80
            self._drag_data['line'].set_ydata([new_y, new_y])
            self._fig.canvas.draw_idle()

    def _on_threshold_release(self, event):
        if self._drag_data['line'] is not None and self._drag_data['freq'] is not None:
            new_thr = self._drag_data['line'].get_ydata()[0]
            freq = self._drag_data['freq']
            if self.core:
                self.core.freq_thresholds[freq] = new_thr
                for i, fvar in enumerate(self._freq_vars):
                    if abs(float(fvar.get()) - freq) < 1e-3:
                        if i in self._thr_vars:
                            self._thr_vars[i].set(f"{new_thr:.1f}")
                        break
                self._log_append(f"Threshold for {freq/1e6:.3f} MHz changed to {new_thr:.1f} dB", tag="info")
        self._drag_data = {'line': None, 'freq': None, 'y0': None}

    # ---------- Settings Tab with Scrollbar ----------
    def _build_settings_tab(self, parent):
        # Очищаем предыдущее содержимое вкладки
        for widget in parent.winfo_children():
            widget.destroy()

        # Создаём Canvas и вертикальный скроллбар
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        # Упаковываем скроллбар и холст
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Внутренний фрейм, который будет содержать все виджеты
        inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor='nw')

        # Функция обновления области прокрутки
        def _on_canvas_configure(event):
            # Обновляем ширину внутреннего фрейма, чтобы он соответствовал ширине холста
            canvas.itemconfig(canvas.find_withtag("all")[0], width=event.width)
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", _on_canvas_configure)

        # ---- ВСЕ ВИДЖЕТЫ ТЕПЕРЬ СОЗДАЮТСЯ ВНУТРИ `inner` ----
        # Заголовок
        tk.Label(inner, text="Frequencies, thresholds and bandwidths", font=("Arial", 12, "bold")).grid(
            row=0, column=0, columnspan=7, sticky=tk.W, pady=(0, 12))

        headers = ["Frequency (Hz)", "On", "Threshold (dB)", "BW, MHz", ""]
        for col, h in enumerate(headers):
            tk.Label(inner, text=h, font=("Arial", 9, "bold"), fg="#555").grid(
                row=1, column=col, padx=8, pady=2, sticky=tk.W)

        for i, fvar in enumerate(self._freq_vars):
            row = i + 2
            tk.Entry(inner, textvariable=fvar, width=14).grid(
                row=row, column=0, padx=8, pady=3)
            tk.Checkbutton(inner, variable=self._freq_enabled[i]).grid(
                row=row, column=1, padx=8)
            tk.Entry(inner, textvariable=self._thr_vars[i], width=8).grid(
                row=row, column=2, padx=8)
            bw_entry = tk.Entry(inner, textvariable=self._bw_vars[i], width=8)
            bw_entry.grid(row=row, column=3, padx=8)
            tk.Button(inner, text="✕", command=lambda idx=i: self._remove_frequency(idx),
                      relief=tk.FLAT, fg="#c0392b").grid(
                row=row, column=4, padx=4, pady=3)

        add_frame = tk.Frame(inner)
        add_frame.grid(row=len(self._freq_vars)+2, column=0, columnspan=7,
                       pady=(16, 4), sticky=tk.W)
        self._new_freq_var = tk.StringVar(value="")
        self._new_thr_var = tk.StringVar(value="-20")
        self._new_bw_var = tk.StringVar(value="8.0")
        tk.Label(add_frame, text="Add freq:").pack(side=tk.LEFT)
        tk.Entry(add_frame, textvariable=self._new_freq_var, width=14).pack(side=tk.LEFT, padx=4)
        tk.Label(add_frame, text="Threshold:").pack(side=tk.LEFT)
        tk.Entry(add_frame, textvariable=self._new_thr_var, width=8).pack(side=tk.LEFT, padx=4)
        tk.Label(add_frame, text="BW (MHz):").pack(side=tk.LEFT)
        tk.Entry(add_frame, textvariable=self._new_bw_var, width=8).pack(side=tk.LEFT, padx=4)
        tk.Button(add_frame, text="+ Add", command=self._add_frequency).pack(side=tk.LEFT, padx=8)

        row_analysis = len(self._freq_vars) + 3
        sep = ttk.Separator(inner, orient=tk.HORIZONTAL)
        sep.grid(row=row_analysis, column=0, columnspan=7, sticky=tk.EW, pady=12)
        row_analysis += 1

        tk.Label(inner, text="Signal Analysis Parameters", font=("Arial", 11, "bold")).grid(
            row=row_analysis, column=0, columnspan=7, sticky=tk.W, pady=(0, 8))
        row_analysis += 1

        analysis_descs = {
            "std_window": "Larger = more stable STD estimate",
            "std_threshold": "Minimum standard deviation for signal",
            "autocorr_threshold": "Higher means more periodic signal",
            "crossings_threshold": "Indicates waveform variability",
            "trend_sensitivity": "Trend threshold for approaching detection",
            "trend_window": "History length for movement estimation",
        }
        for idx, (name, label, default) in enumerate([
            ("std_window", "STD window (points)", "20"),
            ("std_threshold", "STD threshold (dB)", "3.0"),
            ("autocorr_threshold", "Max autocorrelation", "0.6"),
            ("crossings_threshold", "Min zero crossings", "5"),
            ("trend_sensitivity", "Movement sensitivity (dB/step)", "0.3"),
            ("trend_window", "Trend window (points)", "15"),
        ]):
            r = row_analysis + idx
            tk.Label(inner, text=label, font=("Arial", 9)).grid(
                row=r, column=0, sticky=tk.W, padx=8)
            tk.Entry(inner, textvariable=self._analysis_vars[name], width=8).grid(
                row=r, column=1, padx=8, sticky=tk.W)
            tk.Label(inner, text=analysis_descs.get(name, ""), font=("Arial", 8), fg="#666").grid(
                row=r, column=2, columnspan=5, sticky=tk.W, padx=8)

        row_adapt = row_analysis + len(analysis_descs) + 1
        sep = ttk.Separator(inner, orient=tk.HORIZONTAL)
        sep.grid(row=row_adapt, column=0, columnspan=7, sticky=tk.EW, pady=8)
        row_adapt += 1

        tk.Checkbutton(inner, text="Adaptive threshold", variable=self._adaptive_enabled_var).grid(
            row=row_adapt, column=0, sticky=tk.W, padx=8)
        tk.Label(inner, text="Offset (dB):").grid(
            row=row_adapt, column=1, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._adaptive_offset_var, width=8).grid(
            row=row_adapt, column=2, sticky=tk.W, padx=8)
        tk.Label(inner, text="Threshold = noise median + offset", font=("Arial", 8), fg="#666").grid(
            row=row_adapt, column=3, columnspan=4, sticky=tk.W, padx=8)

        row_mode = row_adapt + 1
        tk.Label(inner, text="Scan mode:", font=("Arial", 9)).grid(
            row=row_mode, column=0, sticky=tk.W, padx=8)
        self._mode_combo = ttk.Combobox(inner, values=["normal", "tracking_timeout"], state="readonly", width=15)
        self._mode_combo.set("normal")
        self._mode_combo.grid(row=row_mode, column=1, padx=8, sticky=tk.W)
        tk.Label(inner, text="Tracking timeout (s):").grid(
            row=row_mode, column=2, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._tracking_timeout_var, width=8).grid(
            row=row_mode, column=3, sticky=tk.W, padx=8)

        row_sound = row_mode + 1
        tk.Label(inner, text="Sound volume:", font=("Arial", 9)).grid(
            row=row_sound, column=0, sticky=tk.W, padx=8)
        self._volume_scale = Scale(inner, from_=0.0, to=1.0, resolution=0.05,
                                    orient=tk.HORIZONTAL, length=150,
                                    variable=self._volume_value)
        self._volume_scale.grid(row=row_sound, column=1, columnspan=2, sticky=tk.W, padx=8)
        self._volume_scale.bind("<ButtonRelease-1>", self._on_volume_change)

        row_jam = row_sound + 1
        sep = ttk.Separator(inner, orient=tk.HORIZONTAL)
        sep.grid(row=row_jam, column=0, columnspan=7, sticky=tk.EW, pady=8)
        row_jam += 1

        tk.Label(inner, text="Jamming (experimental!)", font=("Arial", 11, "bold")).grid(
            row=row_jam, column=0, columnspan=7, sticky=tk.W, pady=(0, 8))
        row_jam += 1

        tk.Checkbutton(inner, text="Enable jamming", variable=self._jamming_enabled_var).grid(
            row=row_jam, column=0, columnspan=2, sticky=tk.W, padx=8)

        tk.Label(inner, text="Power (gain 0-40 dB):").grid(
            row=row_jam, column=2, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._jamming_power_var, width=8).grid(
            row=row_jam, column=3, sticky=tk.W, padx=8)

        row_jam += 1
        tk.Label(inner, text="Burst duration (s):").grid(
            row=row_jam, column=0, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._jamming_duration_var, width=8).grid(
            row=row_jam, column=1, sticky=tk.W, padx=8)
        tk.Label(inner, text="Check interval (s):").grid(
            row=row_jam, column=2, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._jamming_check_interval_var, width=8).grid(
            row=row_jam, column=3, sticky=tk.W, padx=8)
        tk.Label(inner, text="(used internally)", font=("Arial", 8), fg="#666").grid(
            row=row_jam, column=4, columnspan=3, sticky=tk.W, padx=8)

        row_email = row_jam + 2
        sep = ttk.Separator(inner, orient=tk.HORIZONTAL)
        sep.grid(row=row_email, column=0, columnspan=7, sticky=tk.EW, pady=8)
        row_email += 1

        tk.Checkbutton(inner, text="Email notifications", variable=self._email_enabled_var).grid(
            row=row_email, column=0, columnspan=2, sticky=tk.W, padx=8)

        email_labels = ["From:", "Password:", "To:", "SMTP server:port"]
        email_keys = ["email_from", "email_password", "email_to", "email_smtp"]
        for i, (label, key) in enumerate(zip(email_labels, email_keys)):
            r = row_email + i + 1
            tk.Label(inner, text=label, font=("Arial", 9)).grid(
                row=r, column=0, sticky=tk.W, padx=8)
            tk.Entry(inner, textvariable=self._email_vars[key], width=30).grid(
                row=r, column=1, columnspan=4, sticky=tk.W, padx=8)

        row_tg = row_email + len(email_labels) + 2
        tk.Checkbutton(inner, text="Telegram notifications", variable=self._telegram_enabled_var).grid(
            row=row_tg, column=0, columnspan=2, sticky=tk.W, padx=8)

        tg_labels = ["Bot Token:", "Chat ID:"]
        tg_keys = ["telegram_token", "telegram_chat_id"]
        for i, (label, key) in enumerate(zip(tg_labels, tg_keys)):
            r = row_tg + i + 1
            tk.Label(inner, text=label, font=("Arial", 9)).grid(
                row=r, column=0, sticky=tk.W, padx=8)
            tk.Entry(inner, textvariable=self._telegram_vars[key], width=30).grid(
                row=r, column=1, columnspan=4, sticky=tk.W, padx=8)

        row_cooldown = row_tg + len(tg_labels) + 2
        tk.Label(inner, text="Alert cooldown (s):").grid(
            row=row_cooldown, column=0, sticky=tk.W, padx=8)
        tk.Entry(inner, textvariable=self._cooldown_var, width=8).grid(
            row=row_cooldown, column=1, padx=8, sticky=tk.W)

        row_apply = row_cooldown + 2
        tk.Button(inner, text="Apply settings",
                  command=self._apply_settings,
                  bg="#2980b9", fg="white", relief=tk.FLAT,
                  font=("Arial", 10, "bold"), padx=12, pady=4).grid(
            row=row_apply, column=0, columnspan=7, pady=16, sticky=tk.W)

        # Обновляем область прокрутки после создания всех виджетов
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_volume_change(self, event):
        if self.core:
            self.core.set_volume(self._volume_value.get())

    # ---------- Add/Remove Frequencies ----------
    def _add_frequency(self):
        try:
            f = float(self._new_freq_var.get())
            t = float(self._new_thr_var.get())
            bw = float(self._new_bw_var.get())
        except ValueError:
            messagebox.showerror("Error", "Enter valid numbers")
            return
        for fvar in self._freq_vars:
            if abs(float(fvar.get()) - f) < 1e-3:
                messagebox.showerror("Error", f"Frequency {f/1e6:.3f} MHz already exists")
                return
        i = len(self._freq_vars)
        self._freq_vars.append(tk.StringVar(value=str(int(f))))
        self._freq_enabled.append(tk.BooleanVar(value=True))
        self._thr_vars[i] = tk.StringVar(value=str(t))
        self._bw_vars[i] = tk.StringVar(value=f"{bw:.1f}")
        self._new_freq_var.set("")
        self._new_bw_var.set("8.0")
        self._refresh_settings_tab()

    def _remove_frequency(self, idx):
        if len(self._freq_vars) <= 1:
            messagebox.showwarning("Warning", "Cannot remove the only frequency")
            return
        del self._freq_vars[idx]
        del self._freq_enabled[idx]
        new_thr = {}
        new_bw = {}
        for key in list(self._thr_vars.keys()):
            if key == idx:
                continue
            new_key = key if key < idx else key - 1
            new_thr[new_key] = self._thr_vars[key]
        for key in list(self._bw_vars.keys()):
            if key == idx:
                continue
            new_key = key if key < idx else key - 1
            new_bw[new_key] = self._bw_vars[key]
        self._thr_vars = new_thr
        self._bw_vars = new_bw
        self._refresh_settings_tab()

    def _refresh_settings_tab(self):
        self._build_settings_tab(self._settings_container)

    # ---------- Apply Settings ----------
    def _apply_settings(self, silent=False):
        if self.running and not silent:
            messagebox.showwarning("Warning", "Stop scanning before changing settings.")
            return

        freqs = []
        thrs = {}
        bws = {}
        for i, fvar in enumerate(self._freq_vars):
            if not self._freq_enabled[i].get():
                continue
            try:
                f = float(fvar.get())
                t = float(self._thr_vars[i].get())
                bw_mhz = float(self._bw_vars[i].get())
            except ValueError:
                if not silent:
                    messagebox.showerror("Error", f"Row {i+1}: invalid format")
                return
            freqs.append(f)
            thrs[f] = t
            bws[f] = bw_mhz * 1e6
        if not freqs:
            if not silent:
                messagebox.showerror("Error", "Select at least one frequency")
            return

        try:
            std_window = int(self._analysis_vars["std_window"].get())
            std_thr = float(self._analysis_vars["std_threshold"].get())
            autocorr_thr = float(self._analysis_vars["autocorr_threshold"].get())
            crossings_thr = float(self._analysis_vars["crossings_threshold"].get())
            trend_sens = float(self._analysis_vars["trend_sensitivity"].get())
            trend_win = int(self._analysis_vars["trend_window"].get())
        except ValueError:
            if not silent:
                messagebox.showerror("Error", "Check analysis parameters (must be numbers)")
            return

        adaptive_en = self._adaptive_enabled_var.get()
        try:
            adaptive_off = float(self._adaptive_offset_var.get())
        except ValueError:
            adaptive_off = 5.0

        mode = self._mode_combo.get()
        try:
            track_timeout = float(self._tracking_timeout_var.get())
        except ValueError:
            track_timeout = 10.0

        try:
            cd = float(self._cooldown_var.get())
        except ValueError:
            cd = 3.0

        email_en = self._email_enabled_var.get()
        email_from = self._email_vars["email_from"].get()
        email_pass = self._email_vars["email_password"].get()
        email_to = self._email_vars["email_to"].get()
        email_smtp = self._email_vars["email_smtp"].get()

        tg_en = self._telegram_enabled_var.get()
        tg_token = self._telegram_vars["telegram_token"].get()
        tg_chat = self._telegram_vars["telegram_chat_id"].get()

        jamming_en = self._jamming_enabled_var.get()
        try:
            jamming_power = int(self._jamming_power_var.get())
        except ValueError:
            jamming_power = 10
        try:
            jamming_duration = float(self._jamming_duration_var.get())
        except ValueError:
            jamming_duration = 2.0
        try:
            jamming_check = float(self._jamming_check_interval_var.get())
        except ValueError:
            jamming_check = 0.5

        if self.core is None:
            self.core = DroneCore(frequencies=freqs, thresholds=thrs, bandwidths=bws)
        else:
            self.core.frequencies = freqs
            self.core.freq_thresholds = thrs
            self.core.bandwidths = bws
            self.core.history = {f: deque(maxlen=HISTORY_LEN) for f in freqs}
            self.core.detection_counts = {f: 0 for f in freqs}
            self.core.noise_history = {f: deque(maxlen=100) for f in freqs}
            for f in freqs:
                self.core.noise_history[f].extend([-65] * 50)

        self.core.detection_cooldown = cd
        self.core.adaptive_threshold_enabled = adaptive_en
        self.core.adaptive_offset = adaptive_off
        self.core.std_window = std_window
        self.core.std_threshold = std_thr
        self.core.autocorr_threshold = autocorr_thr
        self.core.crossings_threshold = crossings_thr
        self.core.trend_sensitivity = trend_sens
        self.core.trend_window = trend_win
        self.core.scan_mode = mode
        self.core.tracking_timeout = track_timeout
        self.core.email_enabled = email_en
        self.core.email_from = email_from
        self.core.email_password = email_pass
        self.core.email_to = email_to
        self.core.email_smtp = email_smtp
        self.core.telegram_enabled = tg_en
        self.core.telegram_token = tg_token
        self.core.telegram_chat_id = tg_chat
        self.core.set_volume(self._volume_value.get())

        self.core.jamming_enabled = jamming_en
        self.core.jamming_power = jamming_power
        self.core.jamming_duration = jamming_duration
        self.core.jamming_check_interval = jamming_check

        self._rebuild_plots(freqs, thrs)
        self._rebuild_tree(freqs)
        self._log_append("Settings applied." if not silent else "Settings loaded.", tag="info")
        self._save_settings()

    # ---------- Rebuild Plots ----------
    def _rebuild_plots(self, freqs, thrs):
        self._fig.clear()
        self._axes = []
        self._lines = []
        self._threshold_lines = []
        self._threshold_artists = []
        colors = ["#1a78c2", "#16a085", "#c0392b", "#8e44ad", "#e67e22", "#27ae60", "#2c3e50"]
        n = len(freqs)
        gs = gridspec.GridSpec(n, 1, figure=self._fig, hspace=0.55)
        for i, freq in enumerate(freqs):
            ax = self._fig.add_subplot(gs[i])
            ax.set_title(f"{freq/1e6:.3f} MHz", fontsize=8, loc="left", pad=2)
            ax.set_ylim(-80, 0)
            ax.set_xlim(0, PLOT_WINDOW)
            ax.set_ylabel("dB", fontsize=7)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3, linewidth=0.5)
            c = colors[i % len(colors)]
            (line,) = ax.plot([], [], color=c, linewidth=1.2)
            thr_line = ax.axhline(
                y=thrs.get(freq, -20),
                color="#e74c3c", linewidth=0.8, linestyle="--", alpha=0.7,
                picker=5
            )
            self._axes.append(ax)
            self._lines.append(line)
            self._threshold_lines.append(thr_line)
            self._threshold_artists.append(thr_line)
        self._canvas.draw()

    def _rebuild_tree(self, freqs):
        for row in self._tree.get_children():
            self._tree.delete(row)
        for freq in freqs:
            self._tree.insert("", tk.END, iid=str(freq),
                              values=(f"{freq/1e6:.3f}", "—", "—", "Waiting"),
                              tags=("ok",))

    # ---------- Statistics Tab ----------
    def _build_stats_tab(self, parent):
        wrapper = tk.Frame(parent, padx=20, pady=20)
        wrapper.pack(fill=tk.BOTH, expand=True)
        tk.Label(wrapper, text="Detection Statistics",
                 font=("Arial", 12, "bold")).pack(anchor=tk.W)
        self._stats_text = tk.Text(wrapper, font=("Courier", 10),
                                   state=tk.DISABLED, height=20)
        self._stats_text.pack(fill=tk.BOTH, expand=True, pady=8)
        tk.Button(wrapper, text="Update",
                  command=self._update_stats).pack(anchor=tk.W)

    def _update_stats(self):
        self._stats_text.config(state=tk.NORMAL)
        self._stats_text.delete("1.0", tk.END)
        if self.core is None:
            self._stats_text.insert(tk.END, "Detector not running.\n")
        else:
            lines = [f"{'Frequency (MHz)':<20} {'Detections':>14}\n", "-"*36+"\n"]
            for f, cnt in self.core.detection_counts.items():
                lines.append(f"{f/1e6:<20.3f} {cnt:>14}\n")
            self._stats_text.insert(tk.END, "".join(lines))
        self._stats_text.config(state=tk.DISABLED)

    # ---------- Control ----------
    def _on_start(self):
        if self.running:
            return
        if self.core is None:
            self.core = DroneCore()
            self._rebuild_tree(self.core.frequencies)

        if not DroneCore.check_hackrf():
            messagebox.showerror("HackRF not found", "Connect HackRF and try again.")
            return

        self.running = True
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._status_var.set("Scanning...")
        self._log_append("Scanning started.", tag="info")

        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        self.root.after(self.SCAN_INTERVAL_MS, self._ui_update_loop)

    def _on_stop(self):
        self.running = False
        if self.core:
            self.core.stop_jamming()
        self._btn_start.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self._status_var.set("Stopped")
        self._mode_var.set("")
        self._log_append("Scanning stopped.", tag="info")
        self._update_stats()

    def _exit_tracking(self):
        if self.core and self.core.tracking_mode:
            self.core.tracking_mode = False
            self.core.tracked_frequency = None
            self.core.track_rssi_history.clear()
            self.core.stop_jamming()
            self._mode_var.set("")
            self._log_append("Exited tracking mode.", tag="info")

    def _on_close(self):
        self.running = False
        if self.core:
            self.core.stop_jamming()
        self.root.destroy()

    # ---------- Scan Thread ----------
    def _scan_loop(self):
        while self.running:
            core = self.core
            if core is None:
                time.sleep(0.1)
                continue

            if core.tracking_mode and core.tracked_frequency is not None:
                data = core.scan_tracking()
                payload = {"mode": "tracking", "data": data}
                if data.get("lost", False) and core.jamming_active:
                    core.stop_jamming()
            else:
                data = core.scan_once()
                now = time.time()
                for freq, info in data.items():
                    if info["alert"] and now - core.last_alert > core.detection_cooldown:
                        core.detection_counts[freq] += 1
                        core.play_alert()
                        core.last_alert = now
                        core.send_email_alert(freq, info["rssi"])
                        core.send_telegram_alert(freq, info["rssi"])
                        mov = info["movement"]
                        if mov and mov["trend"] > core.trend_sensitivity:
                            core.tracking_mode = True
                            core.tracked_frequency = freq
                            core.track_start_time = time.time()
                            core.track_rssi_history.clear()
                            core._build_tracking_sound()
                        elif info["signature"] and info["signature"]["is_drone_like"]:
                            core.tracking_mode = True
                            core.tracked_frequency = freq
                            core.track_start_time = time.time()
                            core.track_rssi_history.clear()
                            core._build_tracking_sound()
                        break
                payload = {"mode": "scan", "data": data}

            with self._lock:
                self._pending = payload
            time.sleep(0.05)

    # ---------- UI Update ----------
    def _ui_update_loop(self):
        if not self.running:
            return
        with self._lock:
            payload = self._pending
            self._pending = None
        if payload is not None:
            if payload["mode"] == "scan":
                self._apply_scan_results(payload["data"])
            else:
                self._apply_tracking_result(payload["data"])
        if self.running:
            self.root.after(self.SCAN_INTERVAL_MS, self._ui_update_loop)

    def _apply_scan_results(self, data):
        core = self.core
        self._mode_var.set("")
        self._status_var.set("Scanning...")

        for i, freq in enumerate(core.frequencies):
            info = data.get(freq)
            if info is None:
                continue
            rssi = info["rssi"]
            thr = info["threshold"]
            alert = info["alert"]
            mov = info["movement"]

            status = "ALERT" if alert else ("OK" if rssi > thr else "—")
            tag = "alert" if alert else "ok"
            try:
                self._tree.item(str(freq), values=(
                    f"{freq/1e6:.3f}", f"{rssi:.1f}", f"{thr:.1f}", status
                ), tags=(tag,))
            except tk.TclError:
                pass

            hist = list(core.history[freq])
            y = hist[-PLOT_WINDOW:] if len(hist) >= PLOT_WINDOW else hist
            x = list(range(len(y)))
            if i < len(self._lines):
                self._lines[i].set_data(x, y)
                self._axes[i].set_xlim(0, max(PLOT_WINDOW, len(y)))
                self._threshold_lines[i].set_ydata([thr, thr])

            if alert:
                conf = info["confidence"]
                mov_str = f" | {mov['speed']}" if mov else ""
                self._log_append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] SIGNAL {freq/1e6:.1f} MHz | "
                    f"RSSI: {rssi:.1f} dB | {conf}{mov_str}",
                    tag="alert"
                )
        try:
            self._canvas.draw_idle()
        except Exception:
            pass

    def _apply_tracking_result(self, data):
        core = self.core
        freq = data["freq"]
        rssi = data["rssi"]
        mov = data["movement"]
        lost = data["lost"]
        jamming = data.get("jamming", False)

        if jamming:
            self._mode_var.set(f"JAMMING {freq/1e6:.1f} MHz")
            self._status_var.set(f"RSSI: {rssi:.1f} dB (jamming)")
            try:
                self._tree.item(str(freq), values=(
                    f"{freq/1e6:.3f}", f"{rssi:.1f}",
                    f"{core.freq_thresholds.get(freq, -20):.1f}",
                    "JAMMING"
                ), tags=("jamming",))
            except tk.TclError:
                pass
            if not hasattr(self, '_last_jam_log') or time.time() - self._last_jam_log > 5:
                self._log_append(f"[{datetime.now().strftime('%H:%M:%S')}] Jamming on {freq/1e6:.1f} MHz (RSSI={rssi:.1f} dB)", tag="jamming")
                self._last_jam_log = time.time()
        else:
            self._mode_var.set(f"TRACKING {freq/1e6:.1f} MHz")
            self._status_var.set(f"RSSI: {rssi:.1f} dB")
            speed_str = mov["speed"] if mov else "—"
            try:
                self._tree.item(str(freq), values=(
                    f"{freq/1e6:.3f}", f"{rssi:.1f}",
                    f"{core.freq_thresholds.get(freq, -20):.1f}",
                    f"TRACKING {speed_str}"
                ), tags=("tracking",))
            except tk.TclError:
                pass

        idx = core.frequencies.index(freq) if freq in core.frequencies else -1
        if 0 <= idx < len(self._lines):
            hist = list(core.history[freq])
            y = hist[-PLOT_WINDOW:] if len(hist) >= PLOT_WINDOW else hist
            self._lines[idx].set_data(range(len(y)), y)
            self._axes[idx].set_xlim(0, max(PLOT_WINDOW, len(y)))
            try:
                self._canvas.draw_idle()
            except Exception:
                pass

        if lost:
            core.tracking_mode = False
            core.tracked_frequency = None
            core.track_rssi_history.clear()
            core.stop_jamming()
            self._mode_var.set("")
            self._log_append(
                f"[{datetime.now().strftime('%H:%M:%S')}] Signal {freq/1e6:.1f} MHz weakened — exiting tracking",
                tag="track"
            )

    def _log_append(self, text, tag="info"):
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n", tag)
        lines = int(self._log.index(tk.END).split(".")[0])
        if lines > self.LOG_MAX:
            self._log.delete("1.0", f"{lines - self.LOG_MAX}.0")
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)


# --------------------- Entry Point ---------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = DroneDetectorGUI(root)
    root.mainloop()
