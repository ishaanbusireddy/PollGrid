/* modes.js — the thematic mode registry. Each mode declares which map tiers
   support it, how its legend formats, and which ramp family it colors with:
   'diverging' ramps use --dem/--rep (data-display partisan colors only),
   'sequential' ramps are derived from the active theme's single --accent. */

const pct = (v) => `${(+v).toFixed(1)}%`;
const margin = (v) => (v > 0 ? `D+${(+v).toFixed(1)}` : v < 0 ? `R+${(-v).toFixed(1)}` : 'EVEN');
const raw = (v) => Number.isInteger(+v) ? String(v) : (+v).toFixed(1);
const dollars = (v) => `$${Number(v) >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : Number(v) >= 1e3 ? (v / 1e3).toFixed(0) + 'K' : raw(v)}`;

/* raceTyped modes are the "value" modes whose backend query is scoped to one
   race type — the HUD's race-type segmented control feeds their race_type=. */
export const MODES = [
  { key: 'partisan_lean',  label: 'Partisan lean (PVI)',  tiers: ['state', 'county', 'district'], ramp: 'diverging',  fmt: margin, raceTyped: true },
  { key: 'forecast',       label: 'Forecast (win prob.)', tiers: ['state', 'county', 'district'], ramp: 'diverging',  fmt: pct,    raceTyped: true },
  { key: 'average_margin', label: 'Polling average margin', tiers: ['state', 'county', 'district'], ramp: 'diverging',  fmt: margin, raceTyped: true },
  { key: 'turnout',        label: 'Turnout / early vote', tiers: ['state', 'county', 'district'], ramp: 'sequential', fmt: pct,    raceTyped: true },
  { key: 'fundraising',    label: 'Fundraising density',  tiers: ['state', 'district'],           ramp: 'sequential', fmt: dollars },
];

/* demo:{category}:{variable} choropleths — every demographic category the
   contract's map/values mode string accepts, one representative variable each */
export const DEMO_MODES = [
  { key: 'demo:education:bachelors_share',       label: 'Education — bachelor’s+ share',   fmt: pct },
  { key: 'demo:economic:median_household_income', label: 'Income — median household',      fmt: dollars },
  { key: 'demo:population_age:median_age',       label: 'Age — median',                    fmt: raw },
  { key: 'demo:housing_urbanicity:owner_share',  label: 'Housing — homeownership share',   fmt: pct },
  { key: 'demo:race_ethnicity:nonwhite_share',   label: 'Race/ethnicity — nonwhite share', fmt: pct },
  { key: 'demo:social_nativity:foreign_born_share', label: 'Foreign-born share',           fmt: pct },
];

for (const m of DEMO_MODES) {
  m.tiers = ['state', 'county', 'district'];
  m.ramp = 'sequential';
}

export const ALL_MODES = [...MODES, ...DEMO_MODES];

export function getMode(key) {
  return ALL_MODES.find((m) => m.key === key) || ALL_MODES[0];
}
