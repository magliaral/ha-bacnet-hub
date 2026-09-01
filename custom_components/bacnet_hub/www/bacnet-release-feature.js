/**
 * custom:bacnet-release-feature — bundled tile feature for BACnet Hub.
 *
 * Renders a full-width release button that releases one priority array
 * slot of a commandable BACnet point via the bacnet_hub.release service.
 * The feature is supported on any entity exposing a priority_array
 * attribute; the button is only enabled while the configured slot is
 * occupied. Served by the integration itself (no manual resource setup).
 */

const DEFAULT_PRIORITY = 8;

/*
 * Custom features register plain strings, so HA's translation JSONs do not
 * apply here — pick the label from the user's frontend language instead.
 */
const _isGerman = () => {
  try {
    const language =
      document.querySelector("home-assistant")?.hass?.locale?.language ||
      navigator.language ||
      "";
    return String(language).toLowerCase().startsWith("de");
  } catch (err) {
    return false;
  }
};

/*
 * Monitor-with-hand-and-slash: the building-automation symbol for a manual
 * override. MDI has no such icon, hence the inline SVG (stroke follows the
 * button's currentColor).
 */
const ICON_SVG = `
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect x="3" y="4" width="18" height="13" rx="2"/>
    <path d="M9 20h6M12 17v3"/>
    <path d="M10.2 8.4v3M12 7.6v3.8M13.8 8.2v3.2"/>
    <path d="M8.9 11.2v1.1c0 1.7 1.4 2.7 3.1 2.7s3.1-1 3.1-2.7v-2.1"/>
    <path d="M3 2.6 21.4 21"/>
  </svg>
`;

class BacnetReleaseFeature extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { priority: DEFAULT_PRIORITY };
  }

  static getStubConfig() {
    return { type: "custom:bacnet-release-feature", priority: DEFAULT_PRIORITY };
  }

  setConfig(config) {
    const priority = Number((config || {}).priority ?? DEFAULT_PRIORITY);
    if (!Number.isInteger(priority) || priority < 1 || priority > 16) {
      throw new Error("bacnet-release-feature: priority must be an integer from 1 to 16");
    }
    this._config = { ...config, priority };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  set stateObj(stateObj) {
    this._stateObj = stateObj;
    this._render();
  }

  _priorityArray() {
    const pa = this._stateObj?.attributes?.priority_array;
    return Array.isArray(pa) ? pa : null;
  }

  _slotOccupied() {
    const pa = this._priorityArray();
    if (!pa) {
      return false;
    }
    const value = pa[this._config.priority - 1];
    return value !== null && value !== undefined;
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        /* Only HA theme variables so the feature blends into light and dark
           themes; the manual override state uses the warning color on
           purpose — an operator is supposed to notice it. */
        button {
          width: 100%;
          height: var(--feature-height, 42px);
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          box-sizing: border-box;
          border: none;
          border-radius: var(--feature-border-radius, 12px);
          font: inherit;
          font-weight: 500;
          line-height: 1;
          cursor: pointer;
          color: var(--warning-color, #ff9800);
          background: rgba(var(--rgb-warning-color, 255, 152, 0), 0.2);
        }
        button:disabled {
          color: var(--disabled-text-color, #bdbdbd);
          background: rgba(var(--rgb-disabled-color, 189, 189, 189), 0.2);
          cursor: not-allowed;
        }
        button svg {
          flex: none;
        }
      </style>
      <button type="button">${ICON_SVG}<span>${_isGerman() ? "Aufheben" : "Release"}</span></button>
    `;
    this._button = this.shadowRoot.querySelector("button");
    this._button.addEventListener("click", () => this._onClick());
  }

  _render() {
    if (!this._stateObj) {
      return;
    }
    if (!this._button) {
      this._build();
    }
    this._button.disabled = !this._slotOccupied();
  }

  _onClick() {
    if (!this._hass || !this._stateObj || !this._slotOccupied()) {
      return;
    }
    this._hass.callService(
      "bacnet_hub",
      "release",
      { priority: this._config.priority },
      { entity_id: this._stateObj.entity_id }
    );
  }
}

const FEATURE_TYPE = "bacnet-release-feature";

const descriptor = {
  type: FEATURE_TYPE,
  get name() {
    // Lazy so the picker sees the language active when it opens, not the
    // one guessed at module load.
    return _isGerman()
      ? "BACnet: Handbedienung aufheben"
      : "BACnet: Release manual override";
  },
  // Supported purely by attribute presence: only writable client points
  // carry priority_array, so ai/bi and publish mirrors drop out on their own.
  supported: (stateObj) =>
    !!stateObj && !!stateObj.attributes && "priority_array" in stateObj.attributes,
  configurable: false,
};

// Register in customTileFeatures ONLY: current frontends merge
// customCardFeatures and customTileFeatures, so pushing into both lists
// would show the feature twice in the picker. The guard keeps a double
// script load from duplicating the entry as well.
window.customTileFeatures = window.customTileFeatures || [];
if (!window.customTileFeatures.some((feature) => feature.type === FEATURE_TYPE)) {
  window.customTileFeatures.push(descriptor);
}

/*
 * Heal stuck "configuration error" placeholders. When a dashboard renders
 * while the element is not defined yet (see _defineWhenReady below), the
 * frontend creates a hui-error-card placeholder that hui-card-feature
 * caches forever — the ll-rebuild HA fires on customElements.whenDefined()
 * is lost while the placeholder is still detached. Shortly after defining,
 * walk the shadow DOM for our placeholders, climb to the containing
 * hui-card and rebuild it via its public load().
 */
const _healStuckPlaceholders = () => {
  try {
    const stuck = [];
    // Every shadow root counts as a level; the path down to a feature's
    // error card crosses well over 25 hops, so keep the bound generous
    // (cycle-safe: the DOM is a tree and dashboards are bounded).
    const walk = (node, depth) => {
      if (!node || depth > 60) {
        return;
      }
      if (node.localName === "hui-error-card") {
        // The placeholder's config is {type: "error", message: "Custom
        // element doesn't exist: bacnet-release-feature."}.
        const message = node._config?.message || "";
        const text = node.shadowRoot?.textContent || node.textContent || "";
        if (message.includes(FEATURE_TYPE) || text.includes(FEATURE_TYPE)) {
          stuck.push(node);
        }
        return;
      }
      if (node.shadowRoot) {
        walk(node.shadowRoot, depth + 1);
      }
      const children = node.children || [];
      for (const child of children) {
        walk(child, depth + 1);
      }
    };
    walk(document.body, 0);
    for (const element of stuck) {
      let node = element;
      for (let hops = 0; node && hops < 40; hops += 1) {
        if (node.localName === "hui-card" && typeof node.load === "function") {
          try {
            node.load();
          } catch (err) {
            console.info("bacnet-release-feature: card rebuild failed:", err);
          }
          break;
        }
        node = node.parentElement || node.getRootNode?.()?.host || null;
      }
    }
    if (stuck.length) {
      console.info(
        `bacnet-release-feature: rebuilt ${stuck.length} card(s) with stuck placeholders`
      );
    }
  } catch (err) {
    console.info("bacnet-release-feature: heal pass failed", err);
  }
};

/*
 * Define LATE, never early: this module is injected into the page head and
 * can execute before HA's main bundle has installed its custom-element
 * registry patch (scoped-custom-element-registry). A define that lands in
 * the native registry before that patch is invisible to HA afterwards —
 * customElements.get() returns undefined forever and every feature renders
 * as a configuration-error placeholder. Waiting until the app's own
 * <home-assistant> element is defined guarantees the patched registry is in
 * place before we register.
 */
let _defineTries = 0;
const _defineWhenReady = () => {
  let appReady = false;
  try {
    appReady = !!window.customElements.get("home-assistant");
  } catch (err) {
    appReady = false;
  }
  if (!appReady && _defineTries < 600) {
    _defineTries += 1;
    setTimeout(_defineWhenReady, 50);
    return;
  }
  if (!window.customElements.get(FEATURE_TYPE)) {
    window.customElements.define(FEATURE_TYPE, BacnetReleaseFeature);
  }
  for (const delay of [300, 1500, 4000, 8000]) {
    setTimeout(_healStuckPlaceholders, delay);
  }
};
_defineWhenReady();

// Version stamp + manual heal hook for support/debugging: run
// window.__bacnetReleaseFeature.heal() in the browser console.
console.info(
  `bacnet-release-feature loaded (${new URL(import.meta.url).search || "no version"})`
);
window.__bacnetReleaseFeature = { heal: _healStuckPlaceholders };
