FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \ 
    libgstrtspserver-1.0-0 \ 
    gstreamer1.0-rtsp \ 
    libgirepository1.0-dev \ 
    gobject-introspection \ 
    gir1.2-gst-rtsp-server-1.0 \ 
    python3-gi \ 
    python3-dev \ 
    python3-gst-1.0 \ 
    && apt-get clean \ 
    && rm -rf /var/lib/apt/lists/*

COPY . /workspace

RUN pip install pyds-1.2.2-cp312-cp312-linux_x86_64.whl
RUN python3 -m pip install -r requirements.txt
RUN unset DISPLAY