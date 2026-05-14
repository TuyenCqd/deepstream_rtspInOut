export CAM_URIS="rtsp://107.120.93.24:9111/mystream"
export CAM_IDS="cam_0"
python3 service/pipeline.py --roi root_config/config_vung_gioi_han.txt --udpport 5400 --rtspport 9112 --pathrtsp /cam1
