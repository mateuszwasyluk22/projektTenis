"""
Aktualizacja danych o nowe mecze.

Jak to dziala:
  1. wrzucasz plik z nowymi meczami do folderu dane/nowe/ (CSV albo XLSX)
  2. python narzedzia/aktualizuj.py
  3. skrypt zamienia kolumny na nasz format, dopasowuje nazwy zawodnikow
     do tych, ktore juz mamy, odrzuca duplikaty i zapisuje dane/atp_matches_nowe.csv
  4. potem odpalasz normalnie projekt.py -> elo.py -> model.py ...

Obslugiwane formaty plikow w dane/nowe/:
  - "tennis-data"  : kolumny Winner, Loser, WRank, LRank, Date, Tournament ...
                     (format ze strony tennis-data.co.uk, opis w notes.txt)
  - "sackmann"     : ten sam format co dane/atp_matches_*.csv
"""
import glob
import os
import unicodedata

import pandas as pd

FOLDER_DANYCH = "dane"
FOLDER_NOWE = "dane/nowe"
PLIK_WYNIKOWY = "dane/atp_matches_nowe.csv"
OKNO_DUPLIKATU_DNI = 14  # ten sam mecz z dwoch zrodel moze miec date o kilka dni inna

# kolejnosc rund - potrzebna zeby mecze z jednego turnieju liczyc po kolei
KOLEJNOSC_RUND = {
    "RR": 0, "R128": 1, "R64": 2, "R32": 3, "R16": 4, "QF": 5, "SF": 6, "BR": 6, "F": 7,
    "1st Round": 2, "2nd Round": 3, "3rd Round": 4, "4th Round": 5,
    "Quarterfinals": 6, "Semifinals": 7, "The Final": 8, "Round Robin": 0,
}

# tennis-data.co.uk -> nazwy kolumn u nas
MAPOWANIE_TENNIS_DATA = {
    "Date": "tourney_date", "Tournament": "tourney_name", "Surface": "surface",
    "Round": "round", "Winner": "winner_name", "Loser": "loser_name",
    "WRank": "winner_rank", "LRank": "loser_rank",
}


# ---------- nazwy zawodnikow ----------

def normalizuj(tekst):
    """male litery, bez akcentow, myslnikow i apostrofow: 'Alex de Miñaur' -> 'alex de minaur'"""
    tekst = unicodedata.normalize("NFKD", str(tekst))
    tekst = "".join(c for c in tekst if not unicodedata.combining(c))
    for znak in "-'.":
        tekst = tekst.replace(znak, " ")
    return " ".join(tekst.lower().split())


def zbuduj_indeks_nazwisk(nazwy):
    """(nazwisko, pierwsza litera imienia) -> lista pelnych nazw, ktore pasuja"""
    indeks = {}
    for pelna in set(nazwy):
        tokeny = normalizuj(pelna).split()
        if len(tokeny) < 2:
            continue
        # nazwisko moze miec kilka czlonow ('del potro'), wiec probujemy kazdy podzial
        for k in range(1, len(tokeny)):
            klucz = (" ".join(tokeny[k:]), tokeny[0][0])
            indeks.setdefault(klucz, set()).add(pelna)
    return indeks


def dopasuj_nazwe(skrot, indeks):
    """'Djokovic N.' -> 'Novak Djokovic'. Zwraca (nazwa, status)."""
    tokeny = normalizuj(skrot).split()
    if len(tokeny) < 2:
        return skrot, "nieznany"
    # ostatni token to inicjaly ('n' albo 'jm'), reszta to nazwisko
    nazwisko, inicjaly = " ".join(tokeny[:-1]), tokeny[-1]
    pasuja = indeks.get((nazwisko, inicjaly[0]), set())
    if len(pasuja) == 1:
        return next(iter(pasuja)), "ok"
    if len(pasuja) > 1:
        return skrot, "niejednoznaczny"
    return skrot, "nowy"


# ---------- wczytywanie ----------

def wczytaj_istniejace():
    pliki = [p for p in glob.glob(FOLDER_DANYCH + "/atp_matches_*.csv") if "nowe" not in p]
    return pd.concat([pd.read_csv(p) for p in pliki], ignore_index=True)


def wczytaj_plik(sciezka):
    if sciezka.endswith((".xlsx", ".xls")):
        return pd.read_excel(sciezka)
    return pd.read_csv(sciezka)


def na_nasz_format(df):
    """Rozpoznaje format po kolumnach i zwraca tabele w formacie Sackmanna."""
    if "winner_name" in df.columns:
        return df.copy(), "sackmann"

    if "Winner" in df.columns:
        # tennis-data: bierzemy tylko mecze rozegrane do konca
        if "Comment" in df.columns:
            df = df[df["Comment"].astype(str).str.lower() == "completed"]
        wynik = df.rename(columns=MAPOWANIE_TENNIS_DATA)[list(MAPOWANIE_TENNIS_DATA.values())].copy()
        wynik["tourney_date"] = pd.to_datetime(wynik["tourney_date"]).dt.strftime("%Y%m%d").astype(int)
        wynik["score"] = "OK"  # projekt.py szuka tylko RET / W/O / DEF
        wynik["tourney_id"] = ""
        return wynik, "tennis-data"

    raise ValueError("Nie rozpoznaje formatu pliku. Kolumny: " + str(list(df.columns)[:12]))


def uzupelnij_wzrost_i_reke(nowe, stare):
    """Nowe zrodla zwykle nie maja wzrostu - bierzemy ostatni znany z naszych danych."""
    stare = stare.sort_values("tourney_date")
    for rola in ["winner", "loser"]:
        for cecha in ["ht", "hand"]:
            kolumna = rola + "_" + cecha
            if kolumna in nowe.columns and nowe[kolumna].notna().all():
                continue
            # slownik: zawodnik -> ostatnia znana wartosc (z obu rol)
            znane = pd.concat([
                stare[["winner_name", "winner_" + cecha]].set_axis(["gracz", "wart"], axis=1),
                stare[["loser_name", "loser_" + cecha]].set_axis(["gracz", "wart"], axis=1),
            ]).dropna().drop_duplicates("gracz", keep="last").set_index("gracz")["wart"]
            nowe[kolumna] = nowe[rola + "_name"].map(znane)
    return nowe


# ---------- duplikaty ----------

def usun_duplikaty(nowe, stare):
    """Mecz uznajemy za ten sam, gdy zwyciezca i przegrany sa ci sami,
    a daty roznia sie o najwyzej OKNO_DUPLIKATU_DNI dni (w jednym turnieju
    ta sama para nie gra dwa razy)."""
    stare = stare[["winner_name", "loser_name", "tourney_date"]].copy()
    stare["d"] = pd.to_datetime(stare["tourney_date"].astype(str), format="%Y%m%d")
    nowe = nowe.copy()
    nowe["d"] = pd.to_datetime(nowe["tourney_date"].astype(str), format="%Y%m%d")
    nowe["_id"] = range(len(nowe))

    pary = nowe[["_id", "winner_name", "loser_name", "d"]].merge(
        stare[["winner_name", "loser_name", "d"]], on=["winner_name", "loser_name"],
        suffixes=("", "_stary"))
    dubel = pary[(pary["d"] - pary["d_stary"]).abs().dt.days <= OKNO_DUPLIKATU_DNI]["_id"].unique()

    # duplikaty wewnatrz samych nowych danych (dwa pliki z tego samego tygodnia)
    nowe = nowe.sort_values(["d", "_id"])
    wewn = nowe.duplicated(["winner_name", "loser_name", "tourney_name", "round"], keep="first")
    odrzucone = set(dubel) | set(nowe.loc[wewn, "_id"])
    return nowe[~nowe["_id"].isin(odrzucone)].drop(columns=["d", "_id"]), len(odrzucone)


# ---------- glowna funkcja ----------

def aktualizuj():
    pliki = sorted(glob.glob(FOLDER_NOWE + "/*.csv") + glob.glob(FOLDER_NOWE + "/*.xlsx"))
    if not pliki:
        print("Brak plikow w", FOLDER_NOWE, "- nie ma czego dokleic.")
        return None

    stare = wczytaj_istniejace()
    ostatnia = pd.to_datetime(stare["tourney_date"].astype(str), format="%Y%m%d").max().date()
    print("Dane do tej pory koncza sie:", ostatnia, "(", len(stare), "meczow )")

    indeks = zbuduj_indeks_nazwisk(list(stare["winner_name"]) + list(stare["loser_name"]))
    tabele = []
    for plik in pliki:
        nowe, format_pliku = na_nasz_format(wczytaj_plik(plik))
        print("Plik", os.path.basename(plik), "-", format_pliku, "-", len(nowe), "meczow")

        if format_pliku == "tennis-data":
            statusy = {"ok": 0, "nowy": 0, "niejednoznaczny": 0}
            for kolumna in ["winner_name", "loser_name"]:
                wyniki = nowe[kolumna].map(lambda n: dopasuj_nazwe(n, indeks))
                nowe[kolumna] = wyniki.map(lambda w: w[0])
                for status in wyniki.map(lambda w: w[1]):
                    statusy[status] += 1
            print("   dopasowanie nazw:", statusy)
            nowe = uzupelnij_wzrost_i_reke(nowe, stare)
        tabele.append(nowe)

    nowe = pd.concat(tabele, ignore_index=True)
    nowe, ile_dubli = usun_duplikaty(nowe, stare)
    print("Odrzucone duplikaty:", ile_dubli, "| nowych meczow:", len(nowe))

    # kolejnosc rund do stabilnego sortowania w elo.py
    nowe["runda_nr"] = nowe["round"].map(KOLEJNOSC_RUND).fillna(0)
    nowe.to_csv(PLIK_WYNIKOWY, index=False)
    print("Zapisano", PLIK_WYNIKOWY)
    return nowe


if __name__ == "__main__":
    aktualizuj()