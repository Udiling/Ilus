import qrcode
from pathlib import Path

def make_qr(data: str, filename: str):
    qr = qrcode.make(data)
    path = Path(filename)
    qr.save(path)
    return str(path)
