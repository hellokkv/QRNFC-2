# Random Qr generation
import qrcode
import os

os.makedirs("qr_codes/drums", exist_ok=True)
os.makedirs("qr_codes/grids", exist_ok=True)

#DrumQR COdes
for i in range(1, 6):
    drum_id = f"D00{i}"
    data = f'{drum_id}'
    img = qrcode.make(data)
    img.save(f"qr_codes/drums/{drum_id}.png")

#GridQR CODE 
for row in "ABC":
    for col in range(1, 4):
        grid_id = f"{row}{col}"
        data = f'{grid_id}'
        img = qrcode.make(data)
        img.save(f"qr_codes/grids/{grid_id}.png")

print("QR codes generated in ./qr_codes/drums and ./qr_codes/grids")
