robimy rozszerzenie do home assistant, które ma mi pomóc optymalizować wykorzystanie energy storage w mieszkaniu.

zaplanuj core i moduły, tak żebysmy sobie mogli rozwijac to rozwiązanie elastycznie o kolejne funkcje. zaplanuj pracę i podziel na etapy. wykorzystaj istniejące biblioteki jesli moga sie do czegos przydać oraz best practice pisania rozerzen do home assistant. jesli cos jest nie jasne, zapytaj i sprobuj uszczegolowic.

program ma zawsze wiedzieć jaka jest cena energi w baterii po stratach.

głównym ekranem programu ma być wykres:
- widoczny soc, zużycie, ładowanie, tryb falownika itp.
i drugi wykres cen:
- cena zakupu energii, cena energii w baterii po stratach itp

potrzebne są różne moduły, między innymi:
ceny - mam taryfę dynamiczną. każdej godziny jest inna cena. mam źródła tych cen - jedne to prognozy (na d+1, d+2, d+3), a drugie to ceny pewne (potwierdzone), zwykle na 24 godziny do przodu publikowane około godz. 11. czyli cena pewna > cena prognozowana. czyli w konfiguracji będą różne źródła cen i potem zajmiemy się konkretnym źródłem (integrcja API)

program na podstawie cen historycznych (np. w okresie ostatnich 3 tygodni powinien mniej więcej rozumieć jak ceny rozkładają się wg godziny i dnia tygodnia - czyli że najtaniej jest od 13 do 16, w nocy i weekendami itp.)

zużycie - program na bieżąco oblicza tygodniowy profil zużycia mieszkania (wskazuję sensor poboru prądu) dla prognozowania zużycia w najbliższych dniach

loads - obciążenia poza profilem zużycia np. EV, prasowanie, pralka, zmywarka

pogoda: pobieranie pogody na najbliższe dni i godzinowej temperatury

ogrzewanie/chłodzenie: mogę dodać źródło ogrzewania / chłodzenia i to ile energii zużywa dziennie w zależnosci od temperatury na zewnątrz; powinno to być uwzględniane w prognozach potem

EV:
- sensor - soc baterii
- sensor - lokalizacja: home / poza home
- konfiguracja: ile km na pełej baterii
- konfiguracja: ile km robię tygodniowo poza kalendarzem
- ładowarka jest 3 fazowa, wpięta przed victorem czyli bezposrednio do sieci (czyli 1 faza jest współdzielona z victronem)

kalendarz:
- odczyt Apple Calendar i pobieranie wydarzeń na najbliższe dni, a następnie obliczanie tras dojazdu km (dom - wydarzenie - dom), a następnie planowanie ładowania
- obsługa wydarzeń godzinowych / całodniowych: wyjazd / prasowanie / pranie -> pranie to np. 3 kWh / h, prasowanie np. 2 kWh / godzinę; wyjazd na kilka dni pozwala utrzymywać SoC baterii w niższym zakresie (czekając na lepsze ceny) oraz powinno mieć prognozę


konfiguracja: 
- moc przyłącza (zabezpieczenie przedlicznikowe np. 32 A) oraz układ 1 lub 3 fazowy
- pojemnosc baterii kWh
- falownik (moc ładowania)
- straty na ładowaniu/rozładowaniu
- koszt użycia baterii za 1 kWh ładowania/rozładowania
- krzywa ładowania: powinienem móc wskazać przedziały z jaką mocą falownik może ładować baterie


wynikiem wyjsciowym programu jest m.in.:
- tryb inwertera: charge / discharge / passthrough
- moc ładowania: full / limited (kiedy ładuje się EV to pobiera 3,5 kW mocy z jednej fazy, więc victron nie może przekraczać reszty, żeby nie wybiło korków)
- grid connected: true / false (false wtedy jak SoC poniżej okreonego XX%)
- ev charge: true / false
- reminders np.: powiadomienie kiedy wrócę do domu i muszę podłączyć samochód do ładowania

inne informacje:
- czasem zdarzają się ceny ujemne, zwykle w weekend. i weekendami ceny są też ogólnie najniższe zwykle. dlatego "w weekend" program powinien whcodzić z możliwie pustą baterią EV i  