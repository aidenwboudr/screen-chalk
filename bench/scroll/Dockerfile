# A private headless Sway (Ubuntu 26.04, Sway 1.11, Mesa: on the host's GPU when it has one) and the apps people
# scroll in: Chrome, VS Code, foot.
# bench/scroll/run.sh builds and uses it.
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      sway foot grim ca-certificates curl gnupg python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
      gir1.2-gtklayershell-0.1 gir1.2-pango-1.0 python3-numpy python3-pil gcc libc6-dev libwayland-dev \
      wayland-protocols pkg-config fonts-dejavu-core fonts-noto-core fonts-liberation xwayland ffmpeg \
      procps dbus-x11 libnss3 libgbm1 libasound2t64 libsecret-1-0 xdg-utils \
      libgl1-mesa-dri libegl1 libgles2 mesa-vulkan-drivers jq zenity wtype \
 && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && curl -fsSL "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-x64" -o /tmp/code.deb \
 && apt-get install -y /tmp/chrome.deb /tmp/code.deb && rm -f /tmp/*.deb && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 -s /bin/bash bench || usermod -l bench -d /home/bench -m $(getent passwd 1000 | cut -d: -f1)
USER bench
WORKDIR /home/bench
