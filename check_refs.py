from utils.references import generate_ref

print(generate_ref("REQ", "QR01"))
print(generate_ref("TXN", "ORDER_100"))
print(generate_ref("LOG", None))
print("Тест последовательности:")
print(generate_ref("SEQ", None))
print(generate_ref("SEQ", None))
print("Проверка завершена. Проверьте файл ref_seq.db")