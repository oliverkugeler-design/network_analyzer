import socket

HOST = "192.168.101.158"   # aktuelle IP der MCC-2
PORT = 5000              # typischer Port (prüfen!)

with socket.create_connection((HOST, PORT), timeout=5) as s:
    # Beispiel: Identifikation abfragen
    s.sendall(b"*IDN?\r\n")
    response = s.recv(4096)
    print(response.decode())