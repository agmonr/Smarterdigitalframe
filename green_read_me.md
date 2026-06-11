# Green Digital Frame: Hardware Preservation & Resource Optimization

DigitalFrame is designed not just for performance, but for **longevity**. We recognize that a digital photo frame is a device intended to run 24/7. To protect your hardware—specifically the Raspberry Pi’s microSD card, the display panel, and network infrastructure—we have implemented several "green" engineering practices.

## 💾 SD Card & I/O Optimization
SD cards have a finite number of write cycles. DigitalFrame aggressively minimizes unnecessary disk writes.

- **RAM-Based State Persistence:** All transient state data—including the currently playing image, sync progress, and temporary status indicators—is stored in **RAM (`/dev/shm`)**. This prevents thousands of tiny daily disk writes that would otherwise degrade your SD card.
- **Config Caching:** Your `config.ini` file is read into memory at startup. Further updates to settings are cached, ensuring the disk is only accessed when settings are explicitly changed.
- **SQLite Performance Tuning:** The system's history database is optimized with **WAL mode** and **memory-mapped I/O**, reducing the frequency and intensity of disk I/O operations.

## 📺 Display Preservation (Burn-in Prevention)
Static images on modern displays can cause permanent burn-in. DigitalFrame actively prevents this.

- **Frameblanking & HDMI Control:** The system utilizes `vcgencmd` to physically cut power to the HDMI output and blank the framebuffer during scheduled quiet hours, extending display life and saving energy.
- **Proximity & Presence Awareness:** Using the camera and motion detection, the system detects when a room is empty and blanking the screen. This drastically reduces total powered-on hours without requiring manual intervention.

## 🌐 Network & Bandwidth Conservation
To ensure your frame is a good neighbor on your home network and respects your data usage.

- **Adaptive Bandwidth Throttling:** The sync engine continuously monitors network throughput. If it detects network congestion, it dynamically throttles download speeds, ensuring background synchronization never degrades your other household devices.
- **Cryptographic Sync Stability:** By using unique image hashes for Google Photos synchronization, the system guarantees that once an image is downloaded, it is recognized even if you reorganize your cloud albums. This prevents the costly re-downloading of hundreds of megabytes of data.
- **Intelligent Video Filtering:** The system uses advanced context-aware scanning of the Google Photos shared album source to detect and **ignore video files and preview thumbnails** completely, saving your bandwidth and storage space for actual photography.

## ☁️ Cloud & Storage Management
We protect your device from running out of storage, which can cause system failures.

- **Storage Breakdown Visualization:** The dashboard provides a real-time, color-coded breakdown of photo storage usage, distinguishing between Google Photos (Blue) and Local Folders (Yellow). This gives users immediate visibility into what is occupying their disk space.
- **1GB Safety Buffer:** The system enforces a strict 1GB free-space safety buffer on your SD card.
- **FIFO Granular Eviction:** When storage is low, the frame doesn't wipe whole folders. It intelligently removes only the oldest downloaded images until the safety buffer is restored, ensuring you have the maximum number of images possible without risking disk-full errors.
- **Playback-Aware Cleanup:** The eviction engine is fully aware of the currently displayed image, guaranteeing it will never delete the photo you are actively watching.

## ⚙️ Performance Tuning for Older Hardware
If you are repurposing an older Raspberry Pi, you can extend its life and performance with these features.

- **Weak Machine Mode:** Available in the settings dashboard, this mode limits refresh intervals and reduces background processing frequency to significantly lower CPU load and power consumption on constrained hardware (like the Raspberry Pi Zero).
- **Service Decoupling:** By separating motion detection, API management, and synchronization into independent lightweight services, we ensure efficient CPU scheduling and prevent any single task from starving the others of resources.
