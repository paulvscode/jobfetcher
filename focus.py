"""Job clusters shown as dedicated sections in the dashboard.

Each cluster groups ROME job codes into families (closest first) and lists the
related business sectors (NAF divisions). collect.py also searches La Bonne
Boîte for every job listed here, so the companies are always available.
"""

FOCUS = {
    "redaction": {
        "title": "Concepteur-rédacteur et métiers voisins",
        "core": "E1119",
        "intro": ("Tous les métiers proches du concepteur-rédacteur : écriture et contenu, création "
                  "publicitaire, communication, social media et web, publicité et médias, marketing, "
                  "édition et presse, rédaction spécialisée."),
        "families": [
            ("Rédaction & conception de contenus", [
                "E1119", "E1104", "E1127", "E1102", "E1134", "E1121", "E1312"]),
            ("Création & direction artistique", [
                "E1128", "E1129", "E1205", "E1207", "E1210", "E1206", "E1211", "E1408"]),
            ("Communication & relations publiques", [
                "E1112", "E1109", "E1103", "E1107", "E1131"]),
            ("Social media, web & marketing digital", [
                "E1101", "E1124", "E1405", "E1407", "M1718", "M1716", "E1406", "M1719",
                "E1113", "M1825", "M1886"]),
            ("Publicité & médias", [
                "E1401", "E1413", "M1702", "E1402", "E1403", "E1404", "E1409", "E1410",
                "E1412", "E1415"]),
            ("Marketing", ["M1705", "M1711", "M1620", "M1703", "M1706"]),
            ("Édition, presse & traduction", [
                "E1105", "E1106", "E1110", "E1114", "E1116", "E1132", "E1108", "E1120",
                "E1123", "E1122"]),
            ("Rédaction spécialisée", ["H1207", "H1234", "H1226"]),
        ],
        # Publicité (73), édition (58), audiovisuel (59, 60), conseil & RP (70),
        # design (74), services d'information (63)
        "naf": ["73", "58", "59", "60", "70", "74", "63"],
    },
}


def focus_romes():
    return sorted({c for f in FOCUS.values() for _, codes in f["families"] for c in codes})
