# Przełączane zasilanie mieszkania i Tarcza Pstryk

Status: projekt do uzgodnienia, 2026-09-12. Nie jest zaimplementowaną funkcją.

Potwierdzone przez użytkownika: każdy licznik ma osobną Tarczę; oba mają
taką samą taryfę (odpowiedź na pytanie obejmujące również dystrybucję).

## Cel i instalacja

Planer ma porównywać łączny koszt obu punktów poboru i proponować ręczne
przełączenia mieszkania tylko wtedy, gdy oszczędność uzasadnia cały cykl.

```text
PPE 1 → Victron + bateria → garaż i pozostałe stałe odbiory
                         → przełącznik → mieszkanie wraz z jego podlicznikami
PPE 2 ───────────────────→ przełącznik
```

PPE 2 nie zasila baterii ani garażu. Victron może zasilać mieszkanie z baterii
lub przepuszczać energię z PPE 1. Stan `victron` nie oznacza więc `battery`.
Pozostałe odbiory przed falownikiem, w tym EV, muszą mieć jawne przypisanie do PPE.

## Rozliczenie i sens ekonomiczny

Według [regulaminu Tarczy Pstryk 2026](https://www.pstryk.pl/regulamin-tarcza-pstryk-2026)
limit dotyczy miesięcznej średniej ważonej kosztu energii wraz z obsługą,
brutto, bez dystrybucji. Regulamin podaje koniec promocji 31.12.2026.
Niezależność rozliczeń obu PPE została potwierdzona przez użytkownika.

Dla niezależnie rozliczanego PPE i miesiąca:

```text
E = import energii w całym miesiącu [kWh]
C = koszt tej energii wraz z obsługą przed Tarczą [PLN brutto]
K = min(C, limit × E) + dystrybucja zmienna + opłaty stałe
```

Limit obejmuje dokładnie składniki z umowy. Trzeba sprawdzić ich zgodność
z obecnym `buy_price`, które obejmuje również akcyzę. Zmiany limitu i okres
obowiązywania muszą być datowane, nie zapisane jako wieczna stała 0,61.

Nie wolno zastąpić tego przez `sum(import[t] × min(cena[t], 0.61))`.
Tarcza na PPE 1 musi działać również w wariancie bez przełączeń.

Ilustracja, wyłącznie energia z obsługą, bez baterii i różnic dystrybucji:

- 100 kWh po 0,30 zł i 10 kWh po 1,20 zł na PPE 1 kosztuje 42 zł.
  Średnia 0,382 zł nie uruchamia Tarczy.
- Po przeniesieniu drogiego poboru na osobno rozliczany, wcześniej nieużywany
  PPE 2: PPE 1 kosztuje 30 zł, PPE 2 kosztuje 6,10 zł po Tarczy.
- Oszczędność w tym przykładzie wynosi 5,90 zł.

Jeżeli oba PPE kończą miesiąc powyżej limitu, a łączny import i dystrybucja
pozostają takie same, samo przeniesienie kWh nie obniża kosztu energii.
W rzeczywistej instalacji zmiana pracy baterii może zmienić import, straty
i późniejsze zakupy, więc wymaga wspólnej optymalizacji.

## Dane wymagane przed rekomendowaniem przełączeń

1. Import PPE 1 i PPE 2 oraz kompletne pokrycie pomiarami od początku miesiąca.
   Zużycie na wyjściu Victrona nie jest importem PPE 1.
2. Koszt energii z obsługą przed Tarczą od początku miesiąca dla każdego PPE
   albo kompletna historia importu i właściwych cen pozwalająca go wyliczyć.
   Należy wybrać jedno źródło rozliczenia, bez zastępowania brakujących danych
   zerami ani innym, słabszym źródłem.
3. Sensor mieszkania mierzący jego zużycie w obu położeniach przełącznika.
4. Rzeczywiste źródło zasilania oraz jego historia. Sposób pozyskania do ustalenia:
   odczyt położenia albo jawnie wybrana deklaracja użytkownika w HA.
   Brak telemetrii nie upoważnia do automatycznego przejścia na deklarację.
5. Taryfa dystrybucyjna każdego PPE, okres obowiązywania Tarczy i sposób jej
   rozliczania. Ta sama taryfa sprzedaży nie oznacza tej samej dystrybucji.
6. Reguły dostępności osoby przełączającej według wybranych kalendarzy.

Brak wymaganych danych daje niedostępną rekomendację z konkretną przyczyną.
Brak pomiaru nie oznacza zerowego poboru; brak rekomendacji nie oznacza
stwierdzonej nieopłacalności. Nie wolno zasilać nowego modelu istniejącymi
zastępnikami ceny lub zużycia bez odrębnej zgody użytkownika.

## Model zużycia i topologia

Obecne `hierarchy.py` i `ConsumptionModule` zakładają jedno stałe drzewo
pod głównym sensorem zużycia. To założenie nie działa po przełączeniu.

- Profil mieszkania wraz z jego potomkami pozostaje niezależny od źródła.
- Profil odbiorów stale za Victronem musi wykluczać mieszkanie tylko w tych
  przedziałach historii, w których pomiar Victrona rzeczywiście je obejmował.
- Przy zasilaniu z PPE 2 nie odejmujemy mieszkania od wyjścia Victrona.
- Nie sumujemy ponownie podliczników zawartych już w sensorze mieszkania.
- Przełączenia wewnątrz godziny wymagają zgodnego czasowo podziału pomiarów;
  nie wolno przypisać całej godziny według stanu na jej końcu. Przy braku
  rozdzielczości potrzebnej do podziału próbka nie nadaje się do uczenia.
- Przepływy importu PPE, wyjścia Victrona, ładowania baterii i zużycia
  mieszkania mają odrębne znaczenie w danych oraz na wykresie.

## Kalendarz i ręczne wykonanie

Istniejący `CalendarModule` udostępnia zdarzenia oraz wyjazdy z czasem dojazdu.
Można wykorzystać ten odczyt, ale dostępność samochodu nie dowodzi dostępności
użytkownika. Potrzebne są wybrane kalendarze i uzgodniona interpretacja zdarzeń.

- Sprawdzamy możliwość wykonania każdego przełączenia, w tym powrotnego.
- Pozostawanie na jednym źródle nie wymaga ciągłej obecności, chyba że
  użytkownik wybierze taką dodatkową regułę.
- Brak zdarzeń oznacza obecność tylko po jawnym przyjęciu takiej konwencji;
  błąd odczytu kalendarza nigdy nie oznacza wolnego terminu.
- Plan zaczyna się od potwierdzonego rzeczywistego stanu przełącznika.
- Planowane przełączenie nie aktualizuje stanu rzeczywistego.
- Po potwierdzeniu przełączenia, spóźnieniu lub zmianie kalendarza następuje
  ponowne planowanie z faktycznego stanu i SoC.
- Powrót z rozpoczętego cyklu nie wymaga ponownego osiągnięcia progu
  opłacalności całego cyklu. Potrzebny jest zapis rozpoczętej operacji.

## Optymalizacja i próg opłacalności

Zmienne wyboru źródła mieszkania i jego zmian powinny być częścią modelu HiGHS
razem z przepływami baterii. Przełączenie odciąża Victrona i może zachować
energię dla garażu lub późniejszych godzin. Ograniczenia mocy obowiązują
osobno dla przyłączy oraz falownika.

Porównujemy dwa przeliczone warianty z tymi samymi danymi:

1. Brak nowego cyklu ręcznych przełączeń, z poprawną Tarczą na obu PPE.
2. Wykonalny harmonogram przełączeń, również z Tarczą na obu PPE.

Po rozpoczęciu cyklu wariant odniesienia musi uwzględniać jego zakończenie,
a nie zakładać ponownego startu z Victrona. Wynik obejmuje zakup energii,
dystrybucję, straty i zużycie baterii oraz jawnie wyceniony stan końcowy baterii.
Zachowana energia nie może być jednocześnie doliczona jako oszczędność
rachunkowa i drugi raz jako korzyść z magazynu.

Propozycja do uzgodnienia: próg 5 zł na cały cykl, minimum 2 godziny na źródle
i najwyżej jeden nowy cykl dziennie. Te wartości nie są jeszcze zatwierdzone.
Próg kwotowy jest podstawą; opcjonalny procent powinien mieć jawny mianownik
i nie zastępować kwoty. Dla kosztu odniesienia <= 0 procent jest niedostępny.

Próg dotyczy każdego dodatkowego cyklu, nie samej sumy zysków harmonogramu.
Ocena marginalna wymaga ponownego przeliczenia planu po usunięciu danego cyklu;
nie wolno sumować niezależnych lokalnych oszczędności przy wspólnej baterii
i nieliniowym rozliczeniu miesięcznym. Remisy powinny premiować mniej zmian.

## Nierozstrzygnięte: miesiąc a horyzont prognozy

Obecny planer ma do 168 godzin. Rozliczenie Tarczy obejmuje pełny miesiąc,
więc historia od jego początku plus tydzień prognozy nie wystarczą do
wyliczenia końcowego rabatu. Późniejsze tanie zakupy mogą zmienić jego wysokość.

Przed wdrożeniem rekomendacji trzeba uzgodnić podstawowy model prognozowania
pozostałej części miesiąca oraz źródło jego danych. Nie implementujemy
domyślnego przedłużenia średnią ceną, stałą ceną 0,61 ani ignorowania reszty
miesiąca. Prognoza rozliczenia musi być opisana jako prognoza, z pokryciem
danych i rozdzieleniem obserwacji od przewidywań. To nie jest obietnica rabatu.
Przejście przez koniec miesiąca tworzy dwa niezależne okresy rozliczenia.

## Proponowane encje i panel

Nazwy robocze; identyfikatory HA zależą od nazwy urządzenia i rejestru encji.

| Encja | Znaczenie |
| --- | --- |
| `sensor.powerpilot_apartment_supply_actual` | Potwierdzone `victron` / `meter_2`; niedostępne bez stanu |
| `sensor.powerpilot_apartment_supply_recommended` | Zalecane źródło teraz |
| `sensor.powerpilot_apartment_supply_plan` | Przedziały zasilania, daty przełączeń, status cyklu |
| `sensor.powerpilot_apartment_next_switch` | Znacznik czasu; atrybuty: źródło docelowe, planowany powrót, identyfikator cyklu |
| `binary_sensor.powerpilot_apartment_switch_required` | Wykonalne teraz przełączenie: stan rzeczywisty różny od zalecanego |
| `binary_sensor.powerpilot_apartment_switch_available` | Dostępność osoby do wykonania zmiany |
| `sensor.powerpilot_apartment_cycle_savings` | Prognozowana oszczędność całego cyklu w PLN |
| `number.powerpilot_apartment_min_cycle_savings` | Ustawiany próg kwotowy |

Oddzielne diagnostyki PPE: energia od początku miesiąca, koszt przed Tarczą,
średnia ważona, prognozowany rabat, kompletność danych i okres rozliczeniowy.
Panel pokazuje oś zasilania mieszkania obok pracy Victrona, porównanie kosztów
wariantów i przyczynę braku rekomendacji. Powiadomienia mogą reagować na
`switch_required` i źródło docelowe; identyfikator cyklu i etapu pozwala uniknąć
powtórek przy każdym odświeżeniu planu. Integracja nie steruje przełącznikiem.

## Zakres zmian i weryfikacja

- `models.py`: przepływy obu PPE, stan i plan mieszkania, rozliczenia miesięcy.
- Nowy moduł rozliczeń Tarczy: historia importu, koszt, okresy i kompletność.
- `hierarchy.py`, `modules/consumption.py`: topologia zależna od rzeczywistego
  zasilania, oddzielne profile stałych i przełączanych odbiorów.
- `modules/calendar.py`: dostępność użytkownika według uzgodnionych reguł.
- `optimizer.py`: źródło mieszkania, bilanse PPE, miesięczny koszt, cykle
  przełączeń, porównanie z wariantem bez nowego cyklu.
- `coordinator.py`, `modules/snapshots.py`, `websocket_api.py`: reakcje na stan,
  historia źródła, rewizje i eksport z przyczynami rekomendacji.
- `config_flow.py`, `const.py`, encje, tłumaczenia i panel: konfiguracja,
  progi oraz plan i diagnostyka.

Testy istotne dla poprawności: średnia miesięczna zamiast limitu godzinowego;
Tarcza na obu PPE; brak zysku przy obu limitach i identycznych przepływach;
zysk przy rozdzieleniu drogich zakupów; koniec miesiąca i ważności promocji;
energia ujemnie wyceniona; niepełna historia; mieszkanie z podlicznikami na
każdym źródle; przełączenie w środku godziny; garaż zawsze za Victronem;
obecność przy obu zmianach; niewykonane przełączenie; restart w trakcie cyklu;
próg całego cyklu i wiele cykli; porównywalny stan końcowy baterii; brak
powiadomień przy niedostępnych danych.

Otwarte ustalenia: pomiary i rzeczywisty stan, konwencja kalendarza,
próg i ograniczenia cyklu, podstawowy model
prognozy do końca miesiąca. Kod rekomendacji zależy od tych odpowiedzi.

## Ustalenia z 2026-09-14

Odpowiedzi użytkownika i regulamin zamykają otwarte punkty powyżej.

### Rozliczenie

- Regulamin Tarczy Pstryk 2026: limit 0,61 zł/kWh brutto (§3 ust. 3),
  liczony jako (koszt energii + opłata za obsługę) / kWh, średnia ważona
  na koniec miesiąca, bez dystrybucji (§3 ust. 1); ważność 01.01.2025 do
  31.12.2026 (§4 ust. 1). Regulamin milczy o rozliczaniu per PPE; osobna
  Tarcza na każdym PPE pozostaje potwierdzeniem użytkownika.
- Rachunek Pstryk ma pozycje: zużycie, koszt energii (TGE), opłata za obsługę
  (0,08 zł/kWh netto, doliczana do każdej kWh), dystrybucja, akcyza
  (0,005 zł/kWh). VAT 23% na wszystkim poza akcyzą (na fakturze akcyza bez VAT).
- Cena pod Tarczę dla godziny: `(tge + markup) × (1 + VAT)`. Akcyza jest poza
  Tarczą. Obecny `buy_price` zawiera akcyzę, więc do porównania z limitem nie
  wolno go użyć wprost; potrzebna osobna wielkość bez akcyzy.
- Do sprawdzenia na fakturze: `pricing.py` nalicza VAT również na akcyzę
  (`(energy_net + excise) × (1 + vat)`), użytkownik podaje akcyzę bez VAT.
  Różnica 0,00115 zł/kWh nie zmienia planu, ale tryb dopasowania do faktury
  co do grosza może się przez to rozjeżdżać. Nie dotyczy Tarczy.
- Dystrybucja PPE 2: ta sama taryfa co PPE 1. Opłaty stałe obu PPE nie zależą
  od przełączeń i nie wchodzą do porównania wariantów.
- Tarcza na PPE 1 wchodzi do funkcji celu planera od pierwszego etapu.
- Reszta miesiąca poza horyzontem cen: tygodniowy profil zużycia i estymata
  cen z archiwum, czyli te same źródła, które dziś wypełniają ogon horyzontu.
  Wynik oznaczony jako prognoza z pokryciem danych.

### Pomiary

- Przed falownikiem na PPE 1 jest wyłącznie ładowarka EV. Import PPE 1 =
  Victron `Energy from AC-in-1 to AC-out` + `Energy from AC-in-1 to inverter`
  + licznik ładowarki EV. Eksport (`Energy from inverter to AC-in-1`) jest
  pomijalny, ale raportowany w diagnostyce.
- PPE 2 zasila wyłącznie mieszkanie, więc import PPE 2 = zużycie mieszkania
  w przedziałach stanu `meter_2`. Brak osobnej encji PPE 2 na żywo; dane
  dostawcy przychodzą z dobowym opóźnieniem i ręcznym eksportem.
- Stan przełącznika wykrywany z mocy. `r` = Victron `Output power L1` minus
  suma mocy innych podliczników za Victronem; `a` = moc mieszkania. Decyzja
  tylko, gdy `a ≥ P_min` (roboczo 300 W): `victron` gdy `r ≥ a − margines`
  (roboczo 100 W), `meter_2` w przeciwnym razie, po N kolejnych zgodnych
  próbkach (roboczo 3). Poza tym obowiązuje ostatni potwierdzony stan,
  zapisany trwale. Przed pierwszym potwierdzeniem stan jest nieznany i
  rekomendacja niedostępna. Progi konfigurowalne. Strefa niejednoznaczna
  (mieszkanie poniżej niezmierzonych odbiorów) jest znana i zaakceptowana.
- Podział godziny z przełączeniem: z historii stanów mocy w recorderze,
  nie z godzinowych statystyk.
- CSV dostawcy (`Data;Strefa;Wartość;Rodzaj;Status`, znacznik to koniec
  godziny: `12:00` oznacza 11:00 do 12:00): osobny etap po Tarczy i
  przełączniku. Użycie: uzgodnienie godzinowe z zapisem PowerPilot per PPE
  z listą rozbieżności oraz rozliczenie zamkniętych miesięcy. Nigdy jako
  źródło bieżącego miesiąca.

### Kalendarz i cykle

- Brak wydarzeń oznacza obecność. Wyjazdy rozpoznane przez moduł kalendarza
  (z dojazdem) oznaczają niedostępność. Inne wydarzenia nie liczą się. Błąd
  odczytu kalendarza oznacza niedostępność.
- Próg kwotowy jako koszt stały otwarcia cyklu w funkcji celu, encja number,
  roboczo 5 zł. Minimalny czas na źródle 2 h (użytkownik: 2 do 3 h), encja
  number. Bez limitu cykli dziennie. Okno ciszy 00:00 do 09:00 bez przełączeń,
  konfigurowalne. Wartości robocze do korekty po obserwacji.

### Uzupełnienia modelu MILP

1. `min(C, limit × E)` jest wklęsłe; w minimalizacji wymaga zmiennej binarnej
   „limit wiąże" na każdą parę PPE i miesiąc, z big‑M. Horyzont przez koniec
   miesiąca daje dwa okresy z osobnymi zmiennymi.
2. Gdy limit wiąże, koszt krańcowy każdej kWh równa się limitowi niezależnie
   od godziny. Arbitraż baterii na składniku energii znika, zostaje
   dystrybucja i zużycie ogniw. Dotyczy PPE 1 od razu, bez przełącznika.
3. `discharge_cap[t]` zależy od tego, czy mieszkanie jest za Victronem, więc
   powstaje iloczyn binarnych `z[t] · s[t]` wymagający linearyzacji.
4. Rozliczenie do końca miesiąca wydłuża horyzont do 31 dni. Obecny zapis SoC
   jako sum prefiksowych ma O(n²) niezerowych współczynników; do zamiany na
   zmienne stanu `E[t]` z równaniem przejścia (rzadkie).
5. Wymaganie osobnego importu PPE 2 jest zbędne przy pkt. „Pomiary";
   zostaje jako kontrola spójności, gdy taka encja kiedyś powstanie.

### Etapy

1. Rozliczenie Tarczy PPE 1: moduł rozliczeń, cena bez akcyzy, sensory
   diagnostyczne, Tarcza w funkcji celu, prognoza reszty miesiąca, refaktor
   SoC na zmienne stanu. **Wdrożone 2026-09-14** — patrz „Stan wdrożenia".
2. Stan przełącznika i topologia: detekcja z mocy, trwała historia stanu,
   profile stałe i przełączane, `hierarchy.py` zależne od stanu.
3. Optymalizacja przełączeń: zmienne źródła, cykle, próg, kalendarz,
   encje, panel, eksport z przyczynami.
4. CSV dostawcy: import, uzgodnienie, zamknięte miesiące.

## Stan wdrożenia etapu 1 (2026-09-14)

- `modules/billing.py`: obserwowane rozliczenie miesiąca PPE 1 z liczników
  `ppe1_import_sensors` (suma; brak odczytu w którymkolwiek = godzina nieznana)
  i ceny pod Tarczę z archiwum; bieżąca godzina z 5-minutowych statystyk.
- `HourSlot.cap_price_kwh` = `(tge + markup) × (1 + VAT)` bez akcyzy, z formuły
  zamrożonej w archiwum; źródło „sensor" nie ma składników → cena niedostępna.
- Estymata cen dla godzin poza zasięgiem archiwum bierze trzy najnowsze
  zarchiwizowane tygodnie tego samego dnia i godziny (wagi 0,5/0,3/0,2);
  wewnątrz zasięgu zachowanie jak dotąd.
- `optimizer.py`: SoC jako zmienne stanu (rzadkie), jawny import `g[t]`,
  ogon miesiąca w modelu (przełączniki ciągłe), oczekiwane ładowanie EV
  w ogonie z profilu jazdy jako obciążenie elastyczne, `min(C, L·E)` per
  okres przez zmienną binarną z big‑M i ponowne rozwiązanie przy ustalonym
  reżimie. Bez Tarczy model jest identyczny z dotychczasowym (testy).
- `Plan.billing`: prognoza miesiąca per okres (energia, koszt przed limitem,
  reżim, koszt po limicie, rabat, podział na obserwacje / plan / ogon) albo
  `unavailable_reason`, gdy limit nie mógł być zamodelowany; bateria jest
  wtedy planowana bez limitu i log to mówi.
- Encje diagnostyczne: `tarcza_ppe1_energy`, `tarcza_ppe1_average`,
  `tarcza_ppe1_forecast_average`, `tarcza_ppe1_discount`,
  `binary_sensor tarcza_ppe1_cap_binds`. Konfiguracja: menu „Tarcza".
- Taryfa prefetchuje dni robocze do końca miesiąca ogona.
- Nie zrobione w tym etapie: panel (wykres), eksport serii w snapshotach,
  CSV dostawcy (etap 4), PPE 2 (etapy 2–3).
