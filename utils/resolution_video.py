import gi
gi.require_version("GstPbutils", "1.0")
from gi.repository import GLib, Gst, GstPbutils

def query_video_resolution(uri): 
    """Query video resolution using GStreamer discoverer Args: uri: Video URI (file:// or rtsp://) Returns: tuple: (width, height) or (None, None) if failed """ 
    try: 
        discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND) 
        info = discoverer.discover_uri(uri) 
        video_streams = info.get_video_streams() 
        if video_streams: 
            video_info = video_streams[0] 
            width = video_info.get_width() 
            height = video_info.get_height() 
            return width, height 
    except Exception as e: 
        print(f"[WARNING] Failed to discover video resolution for {uri}: {e}") 
    return None, None

def get_optimal_streammux_resolution(uris): 
    """Get optimal streammux resolution for all input sources Args: uris: List of video URIs Returns: tuple: (width, height) for streammux """ 
    resolutions = [] 
    print("\n=== Detecting video resolutions ===") 
    for i, uri in enumerate(uris): 
        w, h = query_video_resolution(uri) 
        if w and h: 
            resolutions.append((w, h)) 
            print(f"Source {i}: {uri}") 
            print(f"Resolution: {w}x{h} (aspect ratio: {w/h:.2f})") 
        else: 
            print(f"Source {i}: {uri}") 
            print(f"Resolution: UNKNOWN (will use default)") 
    if not resolutions: 
        print("\n[WARNING] Could not detect any video resolutions!") 
        print("Using default: 1920x1080 (16:9)") 
        return 1920, 1080 
    if len(set(resolutions)) == 1: 
        w, h = resolutions[0] 
        print(f"\nAll sources have same resolution: {w}x{h}") 
        print(f"Using streammux resolution: {w}x{h}") 
        return resolutions[0] 
    aspects = [round(w/h, 2) for w, h in resolutions] 
    if max(aspects) - min(aspects) < 0.01: # Same aspect ratio (tolerance 1%) 
        max_res = max(resolutions, key=lambda x: x[0] * x[1]) 
        print(f"\nAll sources have same aspect ratio: {aspects[0]:.2f}") 
        print(f"Using max resolution: {max_res[0]}x{max_res[1]}") 
        print(f"Note: Smaller videos will be upscaled (no distortion)") 
        return max_res 
    print("\n[WARNING] Input videos have DIFFERENT aspect ratios!") 
    print("This WILL cause image distortion and may significantly affect detection accuracy!") 
    print("\nResolutions detected:") 
    for i, (w, h) in enumerate(resolutions): 
        print(f" Source {i}: {w}x{h} (aspect: {w/h:.2f})") 
    # Use most common aspect ratio 
    aspect_counts = {} 
    for w, h in resolutions: 
        aspect = round(w/h, 2) 
        if aspect not in aspect_counts: 
            aspect_counts[aspect] = [] 
        aspect_counts[aspect].append((w, h)) 
    common_aspect = max(aspect_counts, key=lambda k: len(aspect_counts[k])) 
    common_resolutions = aspect_counts[common_aspect] 
    # Use max resolution with most common aspect ratio 
    selected = max(common_resolutions, key=lambda x: x[0] * x[1]) 
    print(f"\nUsing most common aspect ratio: {common_aspect:.2f}") 
    print(f"Selected resolution: {selected[0]}x{selected[1]}") 
    print(f"\nRecommendation: Convert all videos to same aspect ratio for best results!") 
    return selected