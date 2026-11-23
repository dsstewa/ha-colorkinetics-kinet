"""Reference KiNET encoder (offline only; never sends). Verified against OLA KiNetNodeTest.cpp vectors."""
import struct
MAGIC=0x4ADC0104
def hdr(t,seq,ver=1): return struct.pack('<IHHI',MAGIC,ver,t,seq)
def dmxout(seq,data): return hdr(0x0101,seq)+struct.pack('<BBHIB',0,0,0,0xFFFFFFFF,0)+bytes(data)
def portout(seq,port,data,flags=0,ver=1):
    n=max(len(data),24); return hdr(0x0108,seq,ver)+struct.pack('<IBBHHH',0xFFFFFFFF,port,0,flags,n,0)+bytes(data)+bytes(n-len(data))
if __name__=='__main__':
    d=[1,5,8,10,14,45,100,255]
    assert dmxout(0,d)==bytes([4,1,0xdc,0x4a,1,0,1,1,0,0,0,0,0,0,0,0,255,255,255,255,0]+d)
    assert portout(0,7,d)==bytes([4,1,0xdc,0x4a,1,0,8,1,0,0,0,0,255,255,255,255,7,0,0,0,24,0,0,0]+d+[0]*16)
    assert portout(1,7,list(range(1,25)))==bytes([4,1,0xdc,0x4a,1,0,8,1,1,0,0,0,255,255,255,255,7,0,0,0,24,0,0,0]+list(range(1,25)))
    e=[255]+[0]*23+[255]
    assert portout(2,255,e)==bytes([4,1,0xdc,0x4a,1,0,8,1,2,0,0,0,255,255,255,255,255,0,0,0,25,0,0,0]+e)
    print('all 4 OLA golden vectors match')
