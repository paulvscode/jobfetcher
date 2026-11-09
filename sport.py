"""Settings for the "Salles de sport" tab of the dashboard.

- SPORT_ROMES: sport and fitness jobs. collect.py also collects them for the
  benchmark areas (Rouen, Caen, Brest, Normandy, France) to compare Le Havre.
- SPORT_NAF: business activities (Sirene) counted in each city.
- SPORT_BPE: sports facilities (BPE) counted in each city.
- Competitors (prices, plans, perks, ratings) are in gyms.json, edited by hand.
"""

SPORT_ROMES = {
    "G1217": "Moniteur en salle de sport",
    "G1204": "Éducateur sportif",
    "G1209": "Animateur de loisirs sportifs",
    "G1215": "Éducateur sportif santé",
    "G1220": "Éducateur en activités physiques adaptées (APA)",
    "G1218": "Préparateur physique",
    "G1212": "Animateur socio-sportif",
    "G1213": "Chargé de développement d'activités sportives",
    "G1405": "Directeur de structure sportive",
    "G1406": "Directeur d'équipement sportif",
    "G1224": "Maître-nageur sauveteur",
    "D1211": "Vendeur d'articles de sport",
}

# NAF division of sport and leisure, for offers and hires by sector
SPORT_NAF88 = ["93"]

SPORT_NAF = {
    "93.13Z": "Salles de sport (centres de culture physique)",
    "85.51Z": "Coachs et cours de sport (enseignement sportif)",
    "93.12Z": "Clubs de sport",
    "93.11Z": "Gestion d'installations sportives",
    "93.19Z": "Autres activités liées au sport",
    "96.04Z": "Entretien corporel (spa, bien-être)",
}

SPORT_BPE = {
    "F120": "Salles de remise en forme",
    "F114": "Salles de combat",
    "F129": "Salles spécialisées",
    "F121": "Salles multisports, gymnases",
    "F127": "Murs d'escalade",
    "F101": "Bassins de natation",
    "F109": "Parcours sportifs / santé",
}
