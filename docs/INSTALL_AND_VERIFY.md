# Jak sprawdzić, że PowerPilot działa

Masz trzy poziomy weryfikacji — od najszybszego do „na żywo w HA”.

## 1. Testy logiki (bez Home Assistant) — sekundy

Sprawdzają matematykę baterii, optymalizator, adapter Pradcast i uczenie zużycia:

```bash
python3 scripts/dev_pipeline_test.py      # forecast → optimizer → plan
python3 scripts/dev_pradcast_test.py      # parsowanie cen + profil
python3 scripts/dev_consumption_test.py   # uczenie zużycia (base − urządzenia)
```

Każdy kończy się linią `..._OK`.

## 2. Test integracji w symulowanym HA — ~1 min

Ładuje całą integrację w prawdziwej pętli zdarzeń HA (config flow + setup + encje),
bez stawiania serwera:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -q
```

To realny dowód, że integracja „wstaje” i tworzy encje
(`sensor.powerpilot_inverter_mode`, `sensor.powerpilot_optimization_plan`, …).

## 3. Na żywo w Home Assistant — pełny obraz + dashboard

### 3a. Instalacja integracji
1. Skopiuj katalog `custom_components/powerpilot` do swojej instalacji:
   `…/config/custom_components/powerpilot`
   (albo dodaj to repo w HACS jako *custom repository* → typ *Integration*).
2. Zrestartuj Home Assistant.
3. **Ustawienia → Urządzenia i usługi → Dodaj integrację → PowerPilot**.
4. Przejdź 3 kroki kreatora: rdzeń (sieć/bateria/falownik + sensory) → ceny
   (Pradcast: wklej klucz API) → EV (opcjonalnie).

### 3b. Gdzie zobaczysz, że działa
- **Pasek boczny → PowerPilot** — własny panel integracji (rejestruje się
  automatycznie po instalacji). Trzy zakładki:
  - **Przegląd** — wykresy SoC/zużycie i ceny (z linią „cena energii w baterii”),
    bieżący tryb, koszt horyzontu, przycisk **⚙ Konfiguracja**.
  - **Status** — co działa / czego brakuje (sensory, źródło cen), postęp uczenia
    (godziny w archiwum cen, dni profilu zużycia), stan modułów.
  - **Logi** — ostatnie przeliczenia i ewentualne błędy modułów.
- **Ustawienia → Urządzenia i usługi → PowerPilot** → urządzenie z encjami:
  - `sensor.powerpilot_inverter_mode` (charge/discharge/passthrough)
  - `sensor.powerpilot_charge_power`, `sensor.powerpilot_battery_energy_cost`
  - `sensor.powerpilot_next_action`, `sensor.powerpilot_optimization_plan`
  - `binary_sensor.powerpilot_grid_connected`, `binary_sensor.powerpilot_ev_charge`
- **Narzędzia deweloperskie → Stany** → wpisz `sensor.powerpilot_optimization_plan`
  i rozwiń atrybuty: zobaczysz `hours[]` (decyzje godzinowe), `forecast[]` (ceny,
  zużycie) oraz `price_archive_hours` / `consumption_base_profile`. Jeśli atrybuty
  się wypełniają — pipeline liczy poprawnie.

### 3c. Gdzie pojawi się dashboard
Dashboard to **karta Lovelace**, nie osobny ekran — pokaże się tam, gdzie ją dodasz:

1. Zainstaluj kartę **apexcharts-card**: HACS → Frontend → wyszukaj
   „apexcharts-card” → zainstaluj → odśwież przeglądarkę (Ctrl/Cmd+Shift+R).
2. Otwórz dowolny dashboard → ⋮ (prawy górny róg) → **Edytuj dashboard**.
3. **Dodaj kartę → ręcznie (Manual)** → wklej zawartość
   [dashboards/powerpilot-dashboard.yaml](../dashboards/powerpilot-dashboard.yaml).
   (Jeśli `entity` ma inne ID, podmień `sensor.powerpilot_optimization_plan`.)
4. Zapisz. Zobaczysz dwa wykresy: SoC + przepływy energii oraz ceny z linią
   „Cena energii w baterii po stratach”.

> Wykres pokazuje dane dopiero, gdy `sensor.powerpilot_optimization_plan` ma
> atrybuty `hours`/`forecast` — czyli po pierwszym przeliczeniu (co ~5 min,
> lub od razu po dodaniu integracji).

### 3d. Osobna pozycja w menu bocznym (wykresy + dane + logi)

> **Uwaga:** wbudowany panel **PowerPilot** (sekcja 3b) już daje osobny wpis w
> pasku bocznym z wykresami, statusem i logami — i nie wymaga ApexCharts ani
> edycji `configuration.yaml`. Poniższy dashboard YAML to **opcjonalna
> alternatywa**, jeśli wolisz układ oparty o karty Lovelace/ApexCharts.

Chcesz dodatkowy dashboard Lovelace „PowerPilot” jako wpis w pasku bocznym
(przegląd z wykresami, dane/diagnostyka, logi)? Użyj gotowego pełnego dashboardu
[dashboards/powerpilot-panel.yaml](../dashboards/powerpilot-panel.yaml):

1. Skopiuj plik do `…/config/powerpilot/powerpilot-panel.yaml`.
2. W `configuration.yaml` dodaj (zostaw swój dotychczasowy `mode`):

   ```yaml
   lovelace:
     mode: storage
     dashboards:
       powerpilot:
         mode: yaml
         title: PowerPilot
         icon: mdi:home-battery
         show_in_sidebar: true
         filename: powerpilot/powerpilot-panel.yaml
   ```

3. **Narzędzia deweloperskie → YAML → Sprawdź konfigurację**, potem zrestartuj HA.
4. W pasku bocznym pojawi się **PowerPilot** z trzema zakładkami:
   *Przegląd* (2 wykresy + sterowanie + przypomnienia), *Dane* (wszystkie encje +
   diagnostyka uczenia) oraz *Logi* (historia + logbook).

> Alternatywa bez edycji `configuration.yaml`: **Ustawienia → Dashboardy → Dodaj
> dashboard → Nowy** — nowy dashboard też dostaje wpis w pasku bocznym; potem
> wklej karty z [dashboards/powerpilot-panel.yaml](../dashboards/powerpilot-panel.yaml)
> przez edytor YAML dashboardu.


## Najczęstsze problemy
- **Brak cen / pusty wykres cen** → w kroku „ceny” wybierz Pradcast i wklej *ważny*
  klucz API (sprawdź w Narzędzia deweloperskie, czy `plan` ma `forecast[].buy_price`).
- **Brak uczenia zużycia** → wskazany sensor musi mieć statystyki długoterminowe
  (`state_class`): energia zwykle `total_increasing`, moc `measurement`.
- **„Custom element doesn't exist: apexcharts-card”** → karta nie zainstalowana
  w HACS/Frontend albo brak twardego odświeżenia przeglądarki.
- **Panel PowerPilot nie pojawia się w pasku** → twarde odświeżenie przeglądarki
  (Ctrl/Cmd+Shift+R); panel rejestruje się po starcie integracji.
- **Stare dane się mieszają po zmianach konfiguracji** → Ustawienia → Integracje →
  PowerPilot → **Konfiguruj** → **🧹 Wyczyść dane i cache**. Usuwa archiwum cen,
  wyuczony profil zużycia, snapshoty taryf i symulacje optymalizatora, po czym
  przeładowuje integrację. Konfiguracja (sensory, bateria, taryfy, ceny) zostaje.

## Budowanie frontendu (dla deweloperów)

Panel jest napisany w TypeScript + Lit i budowany esbuildem do jednego pliku
`custom_components/powerpilot/frontend/powerpilot-panel.js` (commitowany, więc
użytkownik nie musi nic budować). Po zmianach w `frontend/src/`:

```bash
cd frontend
npm install
npm run build      # lub: npm run watch
npx tsc --noEmit   # sprawdzenie typów
```

Dane do panelu płyną przez WebSocket API (`powerpilot/plan`, `powerpilot/status`,
`powerpilot/log`) — nie przez atrybuty encji.

