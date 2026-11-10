# # -*- coding: UTF-8 -*-
# # PCM 麦克风实时监听测试
# from audio import Audio
# from machine import Pin

# MIC_DEVICE = 0
# SAMPLE_RATE = 8000
# CHUNK_BYTES = 320
# PLAY_VOLUME = 10
# # PA_ENABLE_PIN = Pin.GPIO33


# def main():
#     # pa_enable = Pin(PA_ENABLE_PIN, Pin.OUT, Pin.PULL_DISABLE, 0)
#     pcm = None
#     try:
#         # pa_enable.write(1)
#         pcm = Audio.PCM(MIC_DEVICE,Audio.PCM.MONO,SAMPLE_RATE,Audio.PCM.WRITEREAD,Audio.PCM.BLOCK,)
        
#         if pcm.setVolume(PLAY_VOLUME) != 0:
#             raise RuntimeError("PCM volume setup failed")
#         print("PCM live monitor started on input %s; press Ctrl+C to stop" % MIC_DEVICE)
#         while True:
#             pcm_buf = pcm.read(CHUNK_BYTES)
#             if pcm_buf == -1 or not pcm_buf:
#                 raise RuntimeError("read failed")
#             pending = bytearray(pcm_buf)
#             while pending:
#                 written = pcm.write(pending)
#                 if written == -1 or written <= 0:
#                     raise RuntimeError("write failed")
#                 pending = pending[written:]

#     finally:
#             if pcm is not None:
#                 pcm.close()



# if __name__ == "__main__":
#     main()
    

''