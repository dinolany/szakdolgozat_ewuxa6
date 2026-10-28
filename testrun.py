import os
from loader import excel_fingerprint, load_excel
from time_transformer import TimeTransformer

print("--- TIMELINE GENERÁLÁS TESZT ---")
print("1. data.xlsx létezik?", os.path.exists("data.xlsx"))

if os.path.exists("data.xlsx"):
    try:
        transformer = TimeTransformer(raw_source=load_excel, output_filename="transformed_timeline.json", source_fingerprint=excel_fingerprint)
        result = transformer.get_timeline(force_rebuild=True)

        print(f"2. Transzformáció lefutott. Eredmény sorok száma: {len(result)}")
        if result:
            print(f"   Idősáv: {result[0]['_virtual_dt']} -> {result[-1]['_virtual_dt']}")
        print("3. transformed_timeline.json létezik?", os.path.exists("transformed_timeline.json"))
        if os.path.exists("transformed_timeline.json"):
            print("Fájl mérete bájtban:", os.path.getsize("transformed_timeline.json"))

    except Exception as e:
        print(f"❌ Hiba történt a futtatás során: {e}")
else:
    print("❌ A data.xlsx fájl nem található a munkakönyvtárban!")
