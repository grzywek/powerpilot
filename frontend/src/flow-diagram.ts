import { LitElement, html, svg, css, nothing, unsafeCSS, type PropertyValues, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import {
  DEVICE_PALETTE,
  ESS_CHARGE_COLOR,
  ESS_DISCHARGE_COLOR,
  EV_COLOR,
  PPE1_COLOR,
  PPE2_COLOR,
} from "./palette";

/** A sensor's state now (powerpilot/flow → topology). */
export interface LiveSensor {
  entity_id: string;
  name: string;
  value: string | null;
  unit: string | null;
}

/** One meter of the tree under the Victron's AC output. */
export interface TreeNode {
  entity_id: string;
  name: string;
  /** The meter's reading in the topology's hour (kWh). */
  kwh: number | null;
  /** Its own energy: the reading minus its direct children. */
  own_kwh: number | null;
  device_index: number | null;
  apartment: boolean;
  climate: boolean;
  /** Not part of the root's reading in that hour (the apartment on meter 2). */
  off_victron: boolean;
  children: TreeNode[];
  power?: LiveSensor | null;
}

export interface TarczaState {
  binds: boolean | null;
  average_kwh: number | null;
  limit_kwh: number;
  terms_name: string;
}

/** What is behind what, what each sensor reads and how the computed values
 *  come out of them — kWh for the last complete hour, W now. */
export interface FlowTopology {
  hour: string;
  supply: { active: boolean; state: string | null; since: string | null; hour_state: string | null };
  ppe1: {
    terms: { entity_id: string; name: string; kwh: number | null }[];
    import_kwh: number | null;
    tarcza: TarczaState | null;
  };
  ppe2: { meter: string; import_kwh: number | null; tarcza: TarczaState | null } | null;
  grid_power: LiveSensor | null;
  ev: {
    behind_meter: boolean;
    soc: number | null;
    plug: LiveSensor | null;
    energy_added: LiveSensor | null;
    energy_added_kwh: number | null;
  } | null;
  battery: {
    soc: LiveSensor | null;
    charge: LiveSensor | null;
    discharge: LiveSensor | null;
    charge_kwh: number | null;
    discharge_kwh: number | null;
    delivered_cost: number | null;
  };
  tree: TreeNode | null;
  detection: {
    apartment: LiveSensor | null;
    victron_output: LiveSensor | null;
    others: (LiveSensor | null)[];
    apartment_w: number | null;
    victron_w: number | null;
    others_w: Record<string, number | null>;
    victron_net_w: number | null;
    min_apartment_w: number;
    victron_share: number;
    confirm_samples: number;
    decision: string | null;
    streak: number | null;
  } | null;
}

/** An energy connection between two cards. */
interface Edge {
  from: string;
  to: string;
  color: string;
  /** Energy flows along it (animated); otherwise drawn as an idle path. */
  active: boolean;
  /** Flows from ``to`` back to ``from`` (the battery discharging). */
  reverse?: boolean;
  /** A derivation (sensor → computed value), not an energy path. */
  derived?: boolean;
  /** Leaves the bottom of ``from`` into the top of ``to`` (same column). */
  vertical?: boolean;
}

interface DrawnEdge extends Edge {
  d: string;
}

const NEUTRAL = "#64748b";
const COMPUTED = "#d97706";
const SWITCH_COLOR = "#0d9488";
const KWH_EPS = 0.005;

const SUPPLY_LABEL: Record<string, string> = {
  victron: "Victron (PPE 1)",
  meter_2: "licznik 2 (PPE 2)",
  mixed: "przełączane w tej godzinie",
  unknown: "nieznane",
};

const kwh = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const watts = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v)} W`);
const pln = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

/** A live reading with its unit, power rounded to whole watts. */
const liveText = (s: LiveSensor | null | undefined): string => {
  if (!s || s.value == null) return "—";
  const n = Number(s.value);
  if (!Number.isFinite(n)) return s.value;
  const unit = s.unit ?? "";
  if (unit === "W") return `${Math.round(n)} W`;
  if (unit === "kW") return `${n.toFixed(2)} kW`;
  if (unit === "%") return `${Math.round(n)} %`;
  return `${n.toFixed(2)}${unit ? ` ${unit}` : ""}`;
};

const deviceColor = (n: TreeNode): string =>
  n.device_index != null ? DEVICE_PALETTE[n.device_index % DEVICE_PALETTE.length] : NEUTRAL;

const shortId = (eid: string) => eid.replace(/^sensor\./, "");

/** Victron meters whose object ids say more than their friendly names. */
const VICTRON_LABELS: [RegExp, string][] = [
  [/from_ac_in_\d+_to_ac_out/, "AC-in → AC-out"],
  [/from_ac_in_\d+_to_inverter/, "AC-in → falownik"],
];

/** A short label from the entity id, as the chart's legend names devices
 *  ("salon_klimka_daily_energy" → "salon klimka"); friendly names are often
 *  just "Energia". The full id is printed under it. */
const label = (eid: string): string => {
  for (const [re, text] of VICTRON_LABELS) if (re.test(eid)) return text;
  const object = eid.split(".").pop() ?? eid;
  const cleaned = object
    .replace(/_(daily_)?(socket_)?(energy|power)(_total|_l\d)?$/, "")
    .replace(/_/g, " ")
    .trim();
  return cleaned || object;
};

/**
 * The installation as a diagram: sources on the left, the meters in the order
 * the energy passes them, the meter tree under the Victron's AC output on the
 * right, and every computed value as a dashed card with its arithmetic.
 *
 * The cards are laid out by CSS (five columns); the energy paths are drawn
 * afterwards from the cards' measured positions, so long names or a narrow
 * screen never misplace them.
 */
@customElement("pp-flow-diagram")
export class PowerPilotFlowDiagram extends LitElement {
  @property({ attribute: false }) topology!: FlowTopology;

  @state() private _edges: DrawnEdge[] = [];
  @state() private _size = { w: 0, h: 0 };

  private _resize?: ResizeObserver;
  private _observed: Element | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    this._resize = new ResizeObserver(() => this._measure());
    this._observed = null;
  }

  disconnectedCallback(): void {
    this._resize?.disconnect();
    super.disconnectedCallback();
  }

  protected updated(changed: PropertyValues): void {
    // The canvas appears with the first data, not with the first render.
    const canvas = this.renderRoot.querySelector(".canvas");
    if (canvas && canvas !== this._observed) {
      if (this._observed) this._resize?.unobserve(this._observed);
      this._resize?.observe(canvas);
      this._observed = canvas;
    }
    if (changed.has("topology")) this._measure();
  }

  /** Energy paths from the cards' positions; re-run on resize and new data. */
  private _measure(): void {
    const canvas = this.renderRoot.querySelector<HTMLElement>(".canvas");
    if (!canvas || !this.topology) return;
    const box = canvas.getBoundingClientRect();
    const rect = (id: string) => {
      const el = this.renderRoot.querySelector<HTMLElement>(`[data-node="${id}"]`);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.left - box.left, y: r.top - box.top, w: r.width, h: r.height };
    };
    const drawn: DrawnEdge[] = [];
    for (const e of this._edgeList()) {
      const a = rect(e.from);
      const b = rect(e.to);
      if (!a || !b) continue;
      let d: string;
      if (e.vertical) {
        const x1 = a.x + a.w / 2;
        const y1 = a.y + a.h;
        const x2 = b.x + b.w / 2;
        const y2 = b.y;
        const dy = Math.max(12, (y2 - y1) / 2);
        d = `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
      } else {
        const x1 = a.x + a.w;
        const y1 = a.y + Math.min(a.h / 2, 34);
        const x2 = b.x;
        const y2 = b.y + Math.min(b.h / 2, 34);
        const dx = Math.max(24, (x2 - x1) / 2);
        d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      }
      drawn.push({ ...e, d });
    }
    const size = { w: canvas.scrollWidth, h: canvas.scrollHeight };
    const key = (list: DrawnEdge[]) => list.map((e) => `${e.d}|${e.active}|${e.reverse}`).join(";");
    if (key(drawn) !== key(this._edges) || size.w !== this._size.w || size.h !== this._size.h) {
      this._edges = drawn;
      this._size = size;
    }
  }

  /** Which cards the energy connects, and whether it flows now. */
  private _edgeList(): Edge[] {
    const t = this.topology;
    const edges: Edge[] = [];
    const gridW = Number(t.grid_power?.value);
    edges.push({ from: "ppe1", to: "victron", color: PPE1_COLOR, active: Number.isFinite(gridW) ? gridW > 5 : (t.ppe1.import_kwh ?? 0) > KWH_EPS });
    if (t.ev) {
      edges.push({ from: "ppe1", to: "ev", color: EV_COLOR, active: (t.ev.energy_added_kwh ?? 0) > KWH_EPS });
    }
    const chg = t.battery.charge_kwh ?? 0;
    const dis = t.battery.discharge_kwh ?? 0;
    edges.push({
      from: "victron",
      to: "battery",
      color: chg >= dis ? ESS_CHARGE_COLOR : ESS_DISCHARGE_COLOR,
      active: Math.max(chg, dis) > KWH_EPS,
      reverse: dis > chg,
      vertical: true,
    });
    const root = t.tree;
    const onMeter2 = t.supply.state === "meter_2";
    if (root) {
      for (const c of root.children) {
        if (c.apartment) continue;
        edges.push({ from: "victron", to: `dev:${c.entity_id}`, color: deviceColor(c), active: (c.kwh ?? 0) > KWH_EPS });
      }
      edges.push({ from: "victron", to: "base", color: COMPUTED, active: false, derived: true });
      const walk = (n: TreeNode) => {
        for (const c of n.children) {
          edges.push({ from: `dev:${n.entity_id}`, to: `dev:${c.entity_id}`, color: deviceColor(c), active: (c.kwh ?? 0) > KWH_EPS });
          walk(c);
        }
        if (n.children.length && n !== root) {
          edges.push({ from: `dev:${n.entity_id}`, to: `own:${n.entity_id}`, color: COMPUTED, active: false, derived: true });
        }
      };
      for (const c of root.children) walk(c);
      const apartment = root.children.find((c) => c.apartment);
      if (apartment && t.supply.active) {
        edges.push({ from: "victron", to: "switch", color: NEUTRAL, active: !onMeter2 });
        edges.push({ from: "ppe2", to: "switch", color: PPE2_COLOR, active: onMeter2 });
        edges.push({ from: "switch", to: `dev:${apartment.entity_id}`, color: onMeter2 ? PPE2_COLOR : NEUTRAL, active: true });
        edges.push({ from: "detection", to: "switch", color: SWITCH_COLOR, active: false, derived: true, vertical: true });
      }
    }
    return edges;
  }

  // ------------------------------------------------------------------ cards

  private _card(
    id: string,
    accent: string,
    icon: string,
    title: string,
    subtitle: string | null,
    body: TemplateResult | typeof nothing,
    opts: { computed?: boolean; dim?: boolean } = {},
  ): TemplateResult {
    return html`<div
      class="card ${opts.computed ? "computed" : ""} ${opts.dim ? "dim" : ""}"
      data-node=${id}
      style="--accent:${accent}"
    >
      <div class="head">
        <span class="icon">${icon}</span>
        <div class="titles">
          <div class="title">${title}</div>
          ${subtitle ? html`<div class="subtitle">${subtitle}</div>` : nothing}
        </div>
      </div>
      ${body}
    </div>`;
  }

  /** One sensor row: label, value, entity id. */
  private _row(label: string, value: string, eid?: string | null, strong = false): TemplateResult {
    return html`<div class="row">
      <span class="label">${label}</span>
      <span class="value ${strong ? "strong" : ""}">${value}</span>
      ${eid ? html`<span class="eid">${shortId(eid)}</span>` : nothing}
    </div>`;
  }

  /** An arithmetic line: "lead = term op term … = result". */
  private _formula(
    lead: string,
    terms: { label: string; value: string; op?: "+" | "−"; struck?: boolean; note?: string }[],
    result: string,
    unit = "kWh",
  ): TemplateResult {
    return html`<div class="formula">
      <span class="lead">${lead}</span><span class="op">=</span>
      ${terms.map(
        (t, i) => html`${i > 0 || t.op === "−"
            ? html`<span class="op">${t.op ?? "+"}</span>`
            : nothing}<span class="term ${t.struck ? "struck" : ""}" title=${t.note ?? ""}
            ><span class="tl">${t.label}</span><span class="tv">${t.value}</span></span
          >`,
      )}
      <span class="op">=</span><span class="term result"><span class="tv">${result}</span><span class="tl">${unit}</span></span>
    </div>`;
  }

  private _tarcza(t: TarczaState | null): TemplateResult | typeof nothing {
    if (!t) return nothing;
    const binds = t.binds;
    const cls = binds == null ? "badge" : binds ? "badge warn" : "badge ok";
    const what =
      binds == null
        ? "rozliczenie nieznane"
        : binds
          ? `wiąże — każda kWh po ${t.limit_kwh.toFixed(2)} zł`
          : "nie wiąże — ceny godzinowe";
    return html`<div class=${cls}>
      Tarcza (${t.terms_name}): średnia miesiąca ${pln(t.average_kwh)} / limit ${t.limit_kwh.toFixed(2)} zł/kWh · ${what}
    </div>`;
  }

  // ---------------------------------------------------------------- columns

  private _gridColumn(t: FlowTopology): TemplateResult {
    const evPrefix = t.ev?.energy_added?.entity_id.split(".").pop()?.split("_")[0];
    const ppe1Terms = t.ppe1.terms.map((term, i) => ({
      note: term.entity_id,
      label:
        evPrefix && (term.entity_id.split(".").pop() ?? "").startsWith(`${evPrefix}_`)
          ? `EV (${label(term.entity_id)})`
          : label(term.entity_id),
      value: kwh(term.kwh),
      op: (i > 0 ? "+" : undefined) as "+" | undefined,
    }));
    return html`<div class="col">
      <div class="col-head">Sieć</div>
      ${this._card(
        "ppe1",
        PPE1_COLOR,
        "⚡",
        "PPE 1",
        "przyłącze główne — za nim ładowarka i Victron",
        html`
          ${this._row("moc z sieci teraz", liveText(t.grid_power), t.grid_power?.entity_id, true)}
          ${t.ppe1.terms.length
            ? html`<div class="sub">Import PPE 1 w godzinie = suma jego liczników</div>
                ${this._formula("import", ppe1Terms, kwh(t.ppe1.import_kwh))}`
            : html`<div class="sub muted">brak liczników importu PPE 1 w opcjach Tarczy</div>`}
          ${this._tarcza(t.ppe1.tarcza)}
        `,
      )}
      <div class="spacer"></div>
      ${t.ppe2
        ? this._card(
            "ppe2",
            PPE2_COLOR,
            "⚡",
            "PPE 2",
            "licznik 2 — zasila tylko mieszkanie, gdy przełączone",
            html`
              <div class="sub">Import PPE 2 w godzinie = licznik mieszkania, gdy mieszkanie było na liczniku 2</div>
              ${this._formula(
                "import",
                [{ label: "licznik mieszkania", value: kwh(t.ppe2.import_kwh), note: t.ppe2.meter }],
                kwh(t.ppe2.import_kwh),
              )}
              <div class="sub">mieszkanie w tej godzinie: <b>${SUPPLY_LABEL[t.supply.hour_state ?? "unknown"] ?? "nieznane"}</b></div>
              ${t.supply.hour_state === "mixed"
                ? html`<div class="sub muted">godzina z przełączeniem: część z 5-minutowych odczytów (Tarcza), tu bez podziału</div>`
                : nothing}
              ${this._tarcza(t.ppe2.tarcza)}
            `,
          )
        : nothing}
    </div>`;
  }

  private _connectionColumn(t: FlowTopology): TemplateResult {
    const b = t.battery;
    const chg = b.charge_kwh ?? 0;
    const dis = b.discharge_kwh ?? 0;
    const batState = Math.max(chg, dis) <= KWH_EPS ? "bez ruchu" : chg >= dis ? "ładowanie" : "rozładowanie";
    return html`<div class="col">
      <div class="col-head">Za PPE 1</div>
      ${t.ev
        ? this._card(
            "ev",
            EV_COLOR,
            "🚗",
            "Ładowarka EV",
            t.ev.behind_meter
              ? "za licznikami domu"
              : "przed licznikami domu — liczona w imporcie PPE 1",
            html`
              ${this._row("ładowarka", liveText(t.ev.plug), t.ev.plug?.entity_id)}
              ${this._row("energia sesji", liveText(t.ev.energy_added), t.ev.energy_added?.entity_id)}
              ${this._row("w godzinie", `${kwh(t.ev.energy_added_kwh)} kWh`, null, true)}
              ${this._row("SoC auta", t.ev.soc != null ? `${Math.round(t.ev.soc)} %` : "—")}
            `,
          )
        : nothing}
      ${this._card(
        "victron",
        "#2563eb",
        "🔌",
        "Victron MultiPlus II",
        "falownik: wejście AC z PPE 1, bateria, wyjście AC do domu",
        html`
          ${this._row("moc z sieci (AC-in)", liveText(t.grid_power), t.grid_power?.entity_id)}
          <div class="section">wyjście AC — korzeń drzewa liczników</div>
          ${t.tree
            ? html`${this._row("zużycie teraz", liveText(t.tree.power), t.tree.entity_id, true)}
                ${this._row("zużycie w godzinie", `${kwh(t.tree.kwh)} kWh`, null, true)}`
            : html`<div class="sub muted">brak czujnika zużycia w opcjach</div>`}
          ${t.detection
            ? this._row("moc wyjścia AC", liveText(t.detection.victron_output), t.detection.victron_output?.entity_id)
            : nothing}
        `,
      )}
      ${this._card(
        "battery",
        chg >= dis ? ESS_CHARGE_COLOR : ESS_DISCHARGE_COLOR,
        "🔋",
        "Bateria ESS",
        `w godzinie: ${batState}`,
        html`
          ${this._row("SoC", liveText(b.soc), b.soc?.entity_id, true)}
          ${this._row("ładowanie", `${kwh(b.charge_kwh)} kWh`, b.charge?.entity_id)}
          ${this._row("rozładowanie", `${kwh(b.discharge_kwh)} kWh`, b.discharge?.entity_id)}
          ${this._row("„Cena w ESS”", `${pln(b.delivered_cost)} zł/kWh`)}
        `,
      )}
    </div>`;
  }

  private _middleColumn(t: FlowTopology): TemplateResult {
    const root = t.tree;
    const det = t.detection;
    const onMeter2 = t.supply.state === "meter_2";
    const hasSwitch = t.supply.active && !!root?.children.some((c) => c.apartment);
    return html`<div class="col">
      <div class="col-head">${hasSwitch ? "Baza i przełącznik" : "Baza"}</div>
      <div class="spacer"></div>
      ${root ? this._baseCard(root) : nothing}
      <div class="spacer"></div>
      ${hasSwitch
        ? html`${det ? this._detectionCard(det) : nothing}
          ${this._card(
            "switch",
            SWITCH_COLOR,
            "🔀",
            "Przełącznik mieszkania",
            "ręczny: Victron (PPE 1) albo licznik 2 (PPE 2)",
            html`
              <div class="switch">
                <span class="pos ${onMeter2 ? "" : "on"}">Victron</span>
                <span class="pos ${onMeter2 ? "on ppe2" : ""}">licznik 2</span>
              </div>
              <div class="sub">
                teraz: <b>${SUPPLY_LABEL[t.supply.state ?? "unknown"] ?? "nieznane"}</b>
                ${t.supply.since
                  ? html` od ${new Date(t.supply.since).toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
                  : nothing}
              </div>
            `,
          )}`
        : nothing}
    </div>`;
  }

  private _detectionCard(det: NonNullable<FlowTopology["detection"]>): TemplateResult {
    const a = det.apartment_w;
    const r = det.victron_net_w;
    const limit = a != null ? det.victron_share * a : null;
    const decided = a != null && a >= det.min_apartment_w;
    const others = det.others.filter((o): o is LiveSensor => o != null);
    return this._card(
      "detection",
      SWITCH_COLOR,
      "🔎",
      "Wykrywanie zasilania",
      `próbka co 30 s, ${det.confirm_samples} zgodnych z rzędu zmienia stan`,
      html`
        ${this._row("a = moc mieszkania", watts(a), null, true)}
        ${this._formula(
          "r",
          [
            { label: "wyjście Victrona", value: watts(det.victron_w) },
            ...others.map((o) => ({ label: label(o.entity_id), value: watts(det.others_w[o.entity_id]), op: "−" as const })),
          ],
          r == null ? "—" : String(Math.round(r)),
          "W",
        )}
        <div class="rule">
          <div class=${decided ? "ok" : "muted"}>
            ${decided ? "✓" : "·"} a ≥ ${Math.round(det.min_apartment_w)} W
            ${decided ? "— próbka rozstrzyga" : "— za mało, próbka nie rozstrzyga"}
          </div>
          ${decided
            ? html`<div>
                r ${r != null && limit != null && r < limit ? "<" : "≥"} ${det.victron_share} · a =
                ${watts(limit)} → <b>${SUPPLY_LABEL[det.decision ?? "unknown"] ?? "—"}</b>
              </div>`
            : nothing}
          ${det.streak ? html`<div class="muted">seria zmiany: ${det.streak} / ${det.confirm_samples}</div>` : nothing}
        </div>
        ${det.apartment ? html`<span class="eid block">a: ${shortId(det.apartment.entity_id)}</span>` : nothing}
        ${det.victron_output ? html`<span class="eid block">r: ${shortId(det.victron_output.entity_id)}${others.map((o) => ` − ${shortId(o.entity_id)}`).join("")}</span>` : nothing}
      `,
    );
  }

  /** A meter of the tree, its reading and — with children — its children. */
  private _deviceCard(n: TreeNode): TemplateResult {
    const color = deviceColor(n);
    const tags = [
      n.apartment ? "mieszkanie" : null,
      n.climate ? "model pogodowy" : null,
      n.off_victron ? "na liczniku 2" : null,
    ].filter(Boolean);
    return this._card(
      `dev:${n.entity_id}`,
      color,
      n.apartment ? "🏠" : n.climate ? "❄️" : "🔌",
      label(n.entity_id),
      tags.length ? tags.join(" · ") : null,
      html`
        ${this._row(n.children.length ? "licznik (z urządzeniami w środku)" : "w godzinie", `${kwh(n.kwh)} kWh`, n.entity_id, true)}
        ${n.apartment && n.power ? this._row("moc teraz", liveText(n.power), n.power.entity_id) : nothing}
      `,
    );
  }

  /** The root's own energy: what no sub-meter measures. */
  private _baseCard(root: TreeNode): TemplateResult {
    const terms = [
      { label: "zużycie AC", value: kwh(root.kwh) },
      ...root.children.map((c) => ({
        label: label(c.entity_id),
        value: kwh(c.kwh),
        op: "−" as const,
        struck: c.off_victron,
        note: c.off_victron ? "na liczniku 2 — nie ma go w odczycie Victrona" : undefined,
      })),
    ];
    return this._card(
      "base",
      COMPUTED,
      "Σ",
      "Baza Victrona",
      "to, czego nie mierzy żaden podlicznik",
      html`
        ${this._formula("baza", terms, kwh(root.own_kwh))}
        ${root.children.some((c) => c.off_victron)
          ? html`<div class="sub muted">przekreślone: na liczniku 2, poza odczytem Victrona — nie odejmujemy</div>`
          : nothing}
      `,
      { computed: true },
    );
  }

  /** A meter's own energy: its reading minus the meters inside it. */
  private _ownCard(n: TreeNode): TemplateResult {
    return this._card(
      `own:${n.entity_id}`,
      COMPUTED,
      "Σ",
      `${label(n.entity_id)} — część własna`,
      "to, czego nie mierzą urządzenia w środku",
      this._formula(
        "własne",
        [
          { label: "licznik", value: kwh(n.kwh) },
          ...n.children.map((c) => ({ label: label(c.entity_id), value: kwh(c.kwh), op: "−" as const })),
        ],
        kwh(n.own_kwh),
      ),
      { computed: true },
    );
  }

  private _loadColumns(t: FlowTopology): TemplateResult {
    const root = t.tree;
    if (!root) return html`<div class="col"></div><div class="col"></div>`;
    const direct = [...root.children].sort((a, b) => Number(a.apartment) - Number(b.apartment));
    const nested = direct.filter((c) => c.children.length);
    const deeper = (n: TreeNode): TemplateResult =>
      html`${n.children.map((c) => html`${this._deviceCard(c)}${c.children.length ? html`${deeper(c)}${this._ownCard(c)}` : nothing}`)}`;
    return html`
      <div class="col">
        <div class="col-head">Odbiorniki</div>
        ${direct.map((c) => this._deviceCard(c))}
      </div>
      <div class="col">
        <div class="col-head">${nested.length === 1 ? `W środku: ${label(nested[0].entity_id)}` : "Liczniki w środku"}</div>
        ${nested.map((n) => html`${deeper(n)}${this._ownCard(n)}`)}
      </div>
    `;
  }

  // ----------------------------------------------------------------- render

  protected render(): TemplateResult {
    const t = this.topology;
    if (!t) return html``;
    const hour = new Date(t.hour);
    const end = new Date(hour.getTime() + 3600_000);
    const hh = (d: Date) => d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    return html`
      <div class="note">
        Odczyty kWh i wszystkie rachunki: ostatnia pełna godzina <b>${hh(hour)}–${hh(end)}</b> ·
        moc (W): teraz · kolory urządzeń jak na wykresie zużycia
      </div>
      <div class="wrap">
        <div class="canvas">
          <svg class="edges" width=${this._size.w} height=${this._size.h} viewBox="0 0 ${this._size.w} ${this._size.h}">
            ${this._edges.map(
              (e) => svg`<path
                class="edge ${e.active ? "active" : ""} ${e.reverse ? "reverse" : ""} ${e.derived ? "derived" : ""}"
                d=${e.d}
                style="--c:${e.color}"
              />`,
            )}
          </svg>
          ${this._gridColumn(t)} ${this._connectionColumn(t)} ${this._middleColumn(t)} ${this._loadColumns(t)}
        </div>
      </div>
      <div class="legend">
        <span><i class="ln active"></i>energia płynie</span>
        <span><i class="ln"></i>połączenie bez przepływu</span>
        <span><i class="ln derived"></i>obliczenie z odczytów</span>
        <span><i class="box computed"></i>wartość liczona przez PowerPilota</span>
        <span><i class="box"></i>czujnik / licznik</span>
      </div>
    `;
  }

  static styles = css`
    :host {
      display: block;
      color: var(--primary-text-color);
      font-size: 12px;
    }
    .note {
      color: var(--secondary-text-color);
      margin: 0 0 10px;
    }
    .wrap {
      overflow-x: auto;
      padding-bottom: 4px;
    }
    .canvas {
      position: relative;
      display: grid;
      grid-template-columns: repeat(5, minmax(190px, 1fr));
      column-gap: 46px;
      min-width: 1130px;
      align-items: stretch;
    }
    .edges {
      position: absolute;
      inset: 0;
      pointer-events: none;
      overflow: visible;
      z-index: 0;
    }
    .edge {
      fill: none;
      stroke: var(--c);
      stroke-width: 1.6;
      stroke-dasharray: 3 5;
      opacity: 0.45;
    }
    .edge.active {
      stroke-width: 2.6;
      stroke-dasharray: 7 7;
      opacity: 0.95;
      animation: flow 1.1s linear infinite;
    }
    .edge.active.reverse {
      animation-direction: reverse;
    }
    .edge.derived {
      stroke-width: 1.4;
      stroke-dasharray: 1 4;
      stroke-linecap: round;
      opacity: 0.8;
      animation: none;
    }
    @keyframes flow {
      to {
        stroke-dashoffset: -28;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .edge.active {
        animation: none;
      }
    }
    .col {
      position: relative;
      z-index: 1;
      display: flex;
      flex-direction: column;
      gap: 14px;
      min-width: 0;
    }
    .col-head {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--secondary-text-color);
      padding: 0 2px;
    }
    .spacer {
      flex: 1 1 auto;
      min-height: 8px;
    }
    .card {
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e5e7eb);
      border-left: 4px solid var(--accent);
      border-radius: 10px;
      padding: 9px 11px 10px;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .card.computed {
      border-style: dashed;
      border-left-style: solid;
      background: color-mix(in srgb, var(--accent) 7%, var(--card-background-color, #fff));
    }
    .card.dim {
      opacity: 0.55;
    }
    .head {
      display: flex;
      gap: 8px;
      align-items: flex-start;
    }
    .icon {
      font-size: 15px;
      line-height: 18px;
      width: 18px;
      text-align: center;
      color: var(--accent);
      font-weight: 700;
    }
    .title {
      font-weight: 600;
      font-size: 13px;
      line-height: 18px;
    }
    .subtitle {
      color: var(--secondary-text-color);
      font-size: 11px;
      line-height: 14px;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr auto;
      column-gap: 8px;
      align-items: baseline;
    }
    .label {
      color: var(--secondary-text-color);
    }
    .value {
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .value.strong {
      font-weight: 600;
    }
    .eid {
      grid-column: 1 / -1;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px;
      color: var(--secondary-text-color);
      opacity: 0.8;
      overflow-wrap: anywhere;
    }
    .eid.block {
      display: block;
    }
    .sub {
      font-size: 11px;
      color: var(--secondary-text-color);
    }
    .section {
      margin-top: 4px;
      padding-top: 5px;
      border-top: 1px dashed var(--divider-color, #e5e7eb);
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      color: var(--secondary-text-color);
    }
    .muted {
      color: var(--secondary-text-color);
      opacity: 0.85;
    }
    .formula {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
    }
    .op {
      color: var(--secondary-text-color);
      font-weight: 600;
    }
    .lead {
      font-weight: 700;
      font-style: italic;
      color: var(--accent);
    }
    .term {
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      padding: 2px 6px;
      border-radius: 6px;
      background: color-mix(in srgb, var(--accent) 10%, transparent);
      line-height: 1.15;
    }
    .term.struck .tv {
      text-decoration: line-through;
      opacity: 0.6;
    }
    .term.result {
      background: var(--accent);
      color: #fff;
    }
    .term.result .tl {
      color: #fff;
      opacity: 0.85;
    }
    .tl {
      font-size: 9.5px;
      color: var(--secondary-text-color);
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tv {
      font-variant-numeric: tabular-nums;
      font-weight: 600;
    }
    .badge {
      font-size: 11px;
      padding: 4px 7px;
      border-radius: 6px;
      background: color-mix(in srgb, var(--secondary-text-color) 10%, transparent);
    }
    .badge.ok {
      background: color-mix(in srgb, #16a34a 12%, transparent);
    }
    .badge.warn {
      background: color-mix(in srgb, #d97706 14%, transparent);
    }
    .switch {
      display: grid;
      grid-template-columns: 1fr 1fr;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--divider-color, #e5e7eb);
    }
    .pos {
      text-align: center;
      padding: 4px 0;
      color: var(--secondary-text-color);
    }
    .pos.on {
      background: ${unsafeCSS(NEUTRAL)};
      color: #fff;
      font-weight: 600;
    }
    .pos.on.ppe2 {
      background: ${unsafeCSS(PPE2_COLOR)};
    }
    .rule {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 4px 6px;
      border-radius: 6px;
      background: color-mix(in srgb, var(--accent) 8%, transparent);
    }
    .rule .ok {
      color: #16a34a;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 16px;
      margin-top: 10px;
      color: var(--secondary-text-color);
      font-size: 11px;
    }
    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .ln {
      display: inline-block;
      width: 26px;
      border-top: 2px dashed ${unsafeCSS(NEUTRAL)};
      opacity: 0.5;
    }
    .ln.active {
      border-top: 3px dashed ${unsafeCSS(PPE1_COLOR)};
      opacity: 1;
    }
    .ln.derived {
      border-top: 2px dotted ${unsafeCSS(COMPUTED)};
      opacity: 1;
    }
    .box {
      display: inline-block;
      width: 14px;
      height: 10px;
      border: 1px solid var(--divider-color, #cbd5e1);
      border-left: 3px solid ${unsafeCSS(NEUTRAL)};
      border-radius: 3px;
    }
    .box.computed {
      border-style: dashed;
      border-left: 3px solid ${unsafeCSS(COMPUTED)};
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "pp-flow-diagram": PowerPilotFlowDiagram;
  }
}
