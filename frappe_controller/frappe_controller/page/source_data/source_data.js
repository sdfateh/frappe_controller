/* Source-data browsing and supported changes go through the Controller. */
frappe.pages["source-data"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({parent: wrapper, title: __("Source Data"), single_column: true});
  wrapper.source_data = new frappe.pages["source-data"].Browser(page);
};

frappe.pages["source-data"].Browser = class SourceDataBrowser {
  constructor(page) {
    this.page = page;
    this.category = "vehicles";
    this.offset = 0;
    this.localOffset = 0;
    this.sequence = 0;
    this.rows = [];
    this.selected = new Map();
    this.filters = ["brand", "model", "year", "structure_type"];
    this.lookupFields = {brands: "brand", models: "model", years: "year", structures: "structureType", engines: "engine"};
    this.labels = {
      brand: __("Brand"), model: __("Model"), year: __("Year"),
      structure_type: __("Body style"), structureType: __("Body style"), engine: __("Engine"),
      colorNameEN: __("Color (English)"), colorNameAR: __("Color (Arabic)"),
      id: __("ID"), tire_name: __("Tire"), tire_size: __("Size"), tire_type: __("Type"),
      section_width: __("Width"), rim_diameter: __("Rim diameter"), vehicleDisplayName: __("Vehicle name"),
      createdAt: __("Created"), updatedAt: __("Updated"),
    };
    this.build();
    this.load();
  }

  build() {
    this.root = $('<div class="controller-source-data"></div>').appendTo(this.page.main);
    this.root.append(`<style>
      .controller-source-data { --source-accent:#1448a4; --source-on-accent:#fff; --source-tint:rgba(20,72,164,.08); --source-surface:#fff; --source-muted:#f5f7f7; --source-line:var(--border-color,#e5e7eb); padding:12px; color:var(--text-color); min-width:0 }
      [data-theme="dark"] .controller-source-data { --source-accent:#8ab4ff; --source-on-accent:#102346; --source-tint:rgba(138,180,255,.12); --source-surface:#202422; --source-muted:#29302e; --source-line:#343b39 }
      .controller-source-data * { box-sizing:border-box }
      .controller-source-data button,.controller-source-data a,.controller-source-data input,.controller-source-data select { touch-action:manipulation }
      .controller-source-data button:focus-visible,.controller-source-data a:focus-visible { outline:2px solid var(--source-accent); outline-offset:3px }
      .controller-source-data .source-icon { width:20px; height:20px; flex:none; stroke:currentColor; fill:none; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round }
      .controller-source-data .source-topbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px 12px; margin-bottom:8px }
      .controller-source-data .source-connection { display:flex; align-items:center; flex-wrap:wrap; gap:5px; font-size:11px; color:var(--text-muted) }
      .controller-source-data .source-connection-dot { width:7px; height:7px; border-radius:50%; background:var(--source-accent) }
      .controller-source-data .source-connection.is-offline .source-connection-dot { background:var(--red-500,#c53838) }
      .controller-source-data .source-connection-origin { max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
      .controller-source-data .source-icon-link { display:inline-flex; gap:5px; align-items:center; color:var(--text-muted); font-size:12px; text-decoration:none; padding:5px }
      .controller-source-data .source-icon-link .source-icon { width:15px; height:15px }
      .controller-source-data .source-categories { display:flex; flex-wrap:wrap; gap:4px; min-width:0 }
      .controller-source-data .source-category { appearance:none; text-align:start; display:flex; align-items:center; gap:6px; border:1px solid var(--source-line); border-radius:7px; padding:6px 10px; min-height:34px; background:var(--source-surface); color:var(--text-color); min-width:0; cursor:pointer; transition:background .15s,border-color .15s }
      .controller-source-data .source-category-icon { width:18px; height:18px; display:flex; justify-content:center; align-items:center; flex:none; color:var(--text-muted) }
      .controller-source-data .source-category-icon .source-icon { width:17px; height:17px }
      .controller-source-data .source-category-copy { min-width:0 }
      .controller-source-data .source-category-label { display:block; font-size:12px; font-weight:600; line-height:1.4 }
      .controller-source-data .source-category:hover { border-color:var(--source-accent) }
      .controller-source-data .source-category.is-active { border-color:var(--source-accent); background:var(--source-tint) }
      .controller-source-data .source-category.is-active .source-category-icon { color:var(--source-accent) }
      .controller-source-data .source-filter-panel { border:1px solid var(--source-line); border-radius:8px; padding:8px 10px; margin-bottom:8px; background:var(--source-surface) }
      .controller-source-data .source-filter-heading { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px }
      .controller-source-data .source-filter-toggle { font-size:12px; display:flex; align-items:center; gap:8px; font-weight:600; background:transparent; border:0; color:var(--text-color); padding:0; cursor:pointer }
      .controller-source-data .source-filter-toggle .source-icon:last-child { width:11px; height:11px; transform:rotate(90deg) }
      .controller-source-data .source-filter-panel:not(.is-expanded) .source-filter-toggle .source-icon:last-child { transform:none }
      .controller-source-data .source-filter-panel:not(.is-expanded) .source-filter-body { display:none }
      .controller-source-data .source-filter-panel:not(.is-expanded) .source-filter-heading { margin-bottom:0 }
      .controller-source-data .source-filter-heading .source-icon { width:15px; height:15px; color:var(--source-accent) }
      .controller-source-data .source-quiet-button { border:0; background:transparent; color:var(--text-muted); font-size:12px; padding:3px 0; cursor:pointer; text-decoration:underline; text-underline-offset:3px }
      .controller-source-data .source-filters { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px }
      .controller-source-data label { font-size:11px; font-weight:500; color:var(--text-muted); margin-bottom:3px }
      .controller-source-data select,.controller-source-data input { min-width:0 }
      .controller-source-data .form-control { border-radius:6px; height:32px; font-size:12px; border:1px solid var(--source-line); background:var(--control-bg,var(--fg-color)); color:var(--text-color) }
      .controller-source-data .form-control:focus { border-color:var(--source-accent); box-shadow:0 0 0 2px var(--source-tint) }
      .controller-source-data .form-control:disabled { opacity:.6 }
      .controller-source-data .source-step { display:inline-block; font-size:10px; color:var(--source-accent); margin-inline-end:6px; font-variant-numeric:tabular-nums }
      .controller-source-data .source-record-filters { display:flex; align-items:flex-end; flex-wrap:wrap; gap:12px }
      .controller-source-data .source-filter-field { flex:1 1 180px; max-width:240px }
      .controller-source-data .source-filter-value { flex:2 1 200px; max-width:380px }
      .controller-source-data .source-filter-note { font-size:11px; margin:6px 0 0; color:var(--text-muted) }
      .controller-source-data .source-toolbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px 10px; margin-bottom:8px }
      .controller-source-data .source-search-wrap { position:relative; flex:1 1 220px; max-width:460px }
      .controller-source-data .source-search-wrap > .source-icon { position:absolute; inset-inline-start:10px; top:8px; z-index:1; width:17px; height:17px; color:var(--text-muted); pointer-events:none }
      .controller-source-data .source-search-wrap input { width:100%; height:34px; padding-inline-start:33px; padding-inline-end:37px; background:var(--source-surface) }
      .controller-source-data .source-search-clear { position:absolute; inset-inline-end:7px; top:6px; border:0; background:transparent; color:var(--text-muted); cursor:pointer; padding:0; width:22px; height:22px }
      .controller-source-data .source-search-clear .source-icon { width:15px; height:15px }
      .controller-source-data .source-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px }
      .controller-source-data .btn { border-radius:6px; font-size:12px; min-height:32px }
      .controller-source-data .source-add { background:var(--source-accent); color:var(--source-on-accent); border-color:var(--source-accent); font-weight:600; padding-inline:16px }
      .controller-source-data .source-add:hover { filter:brightness(.92) }
      .controller-source-data .source-add::before { content:'+'; font-size:17px; line-height:1; margin-inline-end:7px; vertical-align:-1px }
      .controller-source-data .source-reload { display:inline-flex; gap:6px; align-items:center; background:transparent; color:var(--text-muted) }
      .controller-source-data .source-reload .source-icon { width:15px; height:15px }
      .controller-source-data .source-results { min-width:0 }
      .controller-source-data .source-results-heading { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px; margin-bottom:8px }
      .controller-source-data .source-heading-group { display:flex; align-items:center; flex-wrap:wrap; gap:9px }
      .controller-source-data .source-results h3 { font-size:14px; font-weight:600; margin:0; letter-spacing:-.015em }
      .controller-source-data .source-count { font-size:11px; font-weight:500; padding:3px 8px; border-radius:6px; background:var(--source-muted); color:var(--text-muted); font-variant-numeric:tabular-nums }
      .controller-source-data .source-breadcrumb { display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:8px; font-size:12px }
      .controller-source-data .source-breadcrumb:empty { display:none }
      .controller-source-data .source-breadcrumb button { border:0; background:var(--source-tint); color:var(--source-accent); border-radius:6px; padding:4px 8px; max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer }
      .controller-source-data .source-breadcrumb .source-icon { width:12px; height:12px; color:var(--text-muted) }
      .controller-source-data .source-selection { display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:9px 12px; background:var(--source-tint); border:1px solid var(--source-accent); border-radius:9px; margin-bottom:12px; font-size:12px }
      .controller-source-data .source-selection-count { font-weight:600; color:var(--source-accent) }
      .controller-source-data .source-selection .btn { min-height:29px; padding-block:3px; margin-inline-start:auto }
      .controller-source-data .source-table { overflow:auto; max-height:62vh; border:1px solid var(--source-line); border-radius:12px; background:var(--source-surface); scrollbar-width:thin }
      .controller-source-data table { margin:0; width:100%; font-size:12px; border-collapse:separate; border-spacing:0 }
      .controller-source-data th { position:sticky; top:0; background:var(--source-muted); z-index:2; white-space:nowrap; font-size:10px; letter-spacing:.035em; text-transform:uppercase; font-weight:600; color:var(--text-muted); padding:8px 10px; border-bottom:1px solid var(--source-line) }
      .controller-source-data td { min-width:100px; max-width:320px; overflow-wrap:anywhere; vertical-align:middle; padding:9px 10px; border-bottom:1px solid var(--source-line) }
      .controller-source-data tbody tr:last-child td { border-bottom:0 }
      .controller-source-data tbody tr:hover td { background:var(--source-muted) }
      .controller-source-data tbody tr.is-selected td { background:var(--source-tint) }
      .controller-source-data .source-row-check { width:40px; min-width:40px; padding-inline:12px }
      .controller-source-data input[type=checkbox] { width:16px; height:16px; accent-color:var(--source-accent); cursor:pointer; vertical-align:middle }
      .controller-source-data .source-row-id { min-width:75px; color:var(--text-muted); font-variant-numeric:tabular-nums; font-size:11px }
      .controller-source-data .source-row-actions { position:sticky; inset-inline-end:0; z-index:1; background:var(--source-surface); min-width:135px; width:135px; white-space:nowrap; border-inline-start:1px solid var(--source-line) }
      .controller-source-data th.source-row-actions { z-index:3; background:var(--source-muted) }
      .controller-source-data .source-row-actions .btn { min-height:29px; padding:4px 7px; font-size:11px; background:transparent }
      .controller-source-data .source-row-actions .source-edit { color:var(--source-accent); font-weight:600 }
      .controller-source-data .source-row-actions .source-delete { color:var(--text-muted) }
      .controller-source-data .source-row-actions .source-delete:hover { color:var(--red-500,#c53838); background:var(--red-50,#fff0f0) }
      .controller-source-data .source-detail-link { background:transparent; border:0; color:var(--text-color); font-weight:600; text-align:start; padding:0; cursor:pointer; display:flex; gap:9px; align-items:center; line-height:1.5 }
      .controller-source-data .source-detail-link:hover { color:var(--source-accent) }
      .controller-source-data .source-detail-link > .source-icon { width:12px; height:12px; color:var(--text-muted) }
      .controller-source-data .source-record-title { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-width:0 }
      .controller-source-data .source-color-swatch { width:23px; height:23px; border-radius:7px; flex:none; border:1px solid var(--source-line); background:var(--swatch,repeating-conic-gradient(var(--source-line) 0% 25%,var(--source-surface) 0% 50%) 50% / 10px 10px) }
      .controller-source-data .source-lookup-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; padding:2px }
      .controller-source-data .source-table.is-lookups { border:0; border-radius:0; background:transparent }
      .controller-source-data .source-lookup-card { appearance:none; display:flex; align-items:center; gap:8px; padding:9px 10px; background:var(--source-surface); border:1px solid var(--source-line); border-radius:11px; color:var(--text-color); text-align:start; min-width:0; cursor:pointer; transition:border-color .15s,background .15s }
      .controller-source-data .source-lookup-card:not(.is-static):hover { border-color:var(--source-accent); background:var(--source-tint) }
      .controller-source-data .source-lookup-card .source-monogram { display:flex; align-items:center; justify-content:center; width:28px; height:28px; flex:none; border-radius:7px; background:var(--source-tint); color:var(--source-accent); font-size:12px; font-weight:650; text-transform:uppercase }
      .controller-source-data .source-lookup-card .source-value { font-weight:550; font-size:12px; overflow-wrap:anywhere; flex:1; min-width:0 }
      .controller-source-data .source-lookup-card > .source-icon { width:14px; height:14px; color:var(--text-muted) }
      .controller-source-data .source-lookup-card.is-static { cursor:default }
      .controller-source-data .source-empty { border:1px dashed var(--source-line); border-radius:12px; padding:45px 20px; text-align:center; color:var(--text-muted); font-size:13px }
      .controller-source-data .source-empty .source-icon { width:28px; height:28px; margin-bottom:10px; color:var(--source-accent) }
      .controller-source-data .source-empty strong { display:block; font-size:15px; color:var(--text-color); margin-bottom:6px }
      .controller-source-data .source-empty .btn { margin-top:14px }
      .controller-source-data .source-skeleton { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px }
      .controller-source-data .source-skeleton span { height:70px; border-radius:11px; border:1px solid var(--source-line); background:var(--source-muted); animation:source-pulse 1.4s ease-in-out infinite alternate }
      .controller-source-data .source-footer { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; align-items:center; margin-top:10px; font-size:11px; color:var(--text-muted) }
      .controller-source-data .source-pagination { display:flex; gap:7px }
      .controller-source-data .source-pagination .btn { min-height:31px; font-size:11px }
      .controller-source-data .source-meta { border-top:1px solid var(--source-line); margin-top:10px; padding-top:8px; display:flex; flex-wrap:wrap; justify-content:space-between; gap:6px 16px }
      .controller-source-data .source-status { font-size:10px; color:var(--text-muted); margin:0; overflow-wrap:anywhere }
      .controller-source-data .source-cache-note { font-size:10px; color:var(--text-muted); margin:0 }
      @keyframes source-pulse { from { opacity:.45 } to { opacity:1 } }
      @media(min-width:1600px) { .controller-source-data .source-lookup-grid { grid-template-columns:repeat(5,minmax(0,1fr)) } }
      @media(max-width:1150px) { .controller-source-data .source-lookup-grid { grid-template-columns:repeat(3,minmax(0,1fr)) } }
      @media(max-width:700px) {
        .controller-source-data { padding:8px }
        .controller-source-data .source-topbar { gap:4px }
        .controller-source-data .source-categories { display:grid; width:100%; grid-template-columns:repeat(2,minmax(0,1fr)) }
        .controller-source-data .source-connection-origin { max-width:205px }
        .controller-source-data .source-category { padding:6px 8px; gap:6px }
        .controller-source-data .source-category-label { font-size:12px }
        .controller-source-data .source-filters { grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px }
        .controller-source-data .source-filter-panel { padding:7px 9px }
        .controller-source-data .source-search-wrap { max-width:none; flex-basis:100% }
        .controller-source-data .source-actions { width:100% }
        .controller-source-data .source-actions .source-reload { margin-inline-start:auto }
        .controller-source-data .source-actions .source-reload span { display:none }
        .controller-source-data .source-lookup-grid,.controller-source-data .source-skeleton { grid-template-columns:repeat(2,minmax(0,1fr)) }
        .controller-source-data .source-lookup-card { padding:9px 8px; gap:6px }
        .controller-source-data .source-lookup-card .source-monogram { width:29px; height:29px; font-size:10px }
        .controller-source-data .source-lookup-card > .source-icon { display:none }
        .controller-source-data .source-selection { gap:8px; font-size:11px }
        .controller-source-data .source-records-table { display:block; width:100% }
        .controller-source-data .source-records-table thead { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0) }
        .controller-source-data .source-records-table tbody { display:block; padding:8px }
        .controller-source-data .source-records-table tr { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); position:relative; padding:11px 9px; border:1px solid var(--source-line); border-radius:9px; margin-bottom:9px; gap:10px 12px }
        .controller-source-data .source-records-table tr:last-child { margin-bottom:0 }
        .controller-source-data .source-records-table tr.is-selected { border-color:var(--source-accent); background:var(--source-tint) }
        .controller-source-data .source-records-table td { display:block; min-width:0; max-width:none; padding:0; border:0; background:transparent }
        .controller-source-data .source-records-table td[data-label]::before { content:attr(data-label); display:block; font-size:10px; color:var(--text-muted); margin-bottom:3px; font-weight:400 }
        .controller-source-data .source-records-table td.source-row-check { position:absolute; top:12px; inset-inline-end:11px; width:20px }
        .controller-source-data .source-records-table td.source-primary { grid-column:1 / -1 }
        .controller-source-data .source-records-table td.source-row-actions { display:flex; position:static; width:auto; grid-column:1 / -1; gap:8px; padding-top:8px; border-top:1px solid var(--source-line); background:transparent }
        .controller-source-data .source-records-table td.source-row-actions .btn { padding:4px 9px }
        .controller-source-data .source-records-table tr:hover td { background:transparent }
        .controller-source-data .source-meta { flex-direction:column }
      }
      @media(prefers-reduced-motion:reduce) { .controller-source-data * { animation:none !important; transition:none !important } }
    </style>`);
    const topbar = $('<div class="source-topbar"></div>').appendTo(this.root);
    const categories = $('<nav class="source-categories"></nav>').attr("aria-label", __("Source catalogs")).appendTo(topbar);
    this.connection = $('<div class="source-connection"></div>').appendTo(topbar);
    $('<span class="source-connection-dot" aria-hidden="true"></span>').appendTo(this.connection);
    this.connectionOrigin = $('<span class="source-connection-origin"></span>').text(__("Connecting…")).appendTo(this.connection);
    this.apiDocumentation = $('<a class="source-icon-link" target="_blank" rel="noopener noreferrer"></a>')
      .attr({"aria-label": __("API documentation"), "title": __("API documentation")}).append(this.icon("external")).hide().appendTo(this.connection);
    this.settingsLink = $('<a class="source-icon-link" href="/desk/frappe-controller-settings"></a>')
      .attr("aria-label", __("Source Data settings")).attr("title", __("Source Data settings")).append(this.icon("settings")).hide().appendTo(this.connection);
    this.categoryButtons = {};
    for (const [value, label, icon] of [
      ["vehicles", __("Vehicles & brands"), "car"],
      ["vehicle_records", __("Vehicle records"), "list"],
      ["tires", __("Tires"), "wheel"],
      ["colors", __("Colors"), "palette"],
    ]) {
      const button = $('<button type="button" class="source-category"></button>')
        .attr({"aria-label": label, "aria-pressed": value === this.category ? "true" : "false"})
        .toggleClass("is-active", value === this.category).appendTo(categories).on("click", () => this.changeCategory(value));
      $('<span class="source-category-icon" aria-hidden="true"></span>').append(this.icon(icon)).appendTo(button);
      const copy = $('<span class="source-category-copy"></span>').appendTo(button);
      $('<span class="source-category-label"></span>').text(label).appendTo(copy);
      this.categoryButtons[value] = button;
    }
    const panel = $('<section class="source-filter-panel"></section>').attr("aria-label", __("Catalog filters")).appendTo(this.root);
    const filterHeading = $('<div class="source-filter-heading"></div>').appendTo(panel);
    const filterTitle = $('<button type="button" class="source-filter-toggle" aria-controls="source-filter-body"></button>').append(this.icon("filter")).appendTo(filterHeading);
    this.filterTitle = $('<span></span>').text(__("Explore a vehicle")).appendTo(filterTitle);
    filterTitle.append(this.icon("arrow"));
    const setFilterExpanded = expanded => { panel.toggleClass("is-expanded", expanded); filterTitle.attr("aria-expanded", String(expanded)); };
    filterTitle.on("click", () => setFilterExpanded(!panel.hasClass("is-expanded")));
    setFilterExpanded(false);
    $('<button type="button" class="source-quiet-button"></button>').text(__("Reset filters")).appendTo(filterHeading).on("click", () => this.clearFilters());
    const filterBody = $('<div id="source-filter-body" class="source-filter-body"></div>').appendTo(panel);
    this.filterArea = $('<div class="source-filters"></div>').appendTo(filterBody);
    this.selects = {};
    this.filters.forEach((key, index) => {
      const group = $('<div></div>').appendTo(this.filterArea);
      const label = $('<label></label>').attr("for", `source-data-${key}`).appendTo(group);
      $('<span class="source-step" aria-hidden="true"></span>').text(String(index + 1).padStart(2, "0")).appendTo(label);
      label.append(document.createTextNode(this.labels[key]));
      this.selects[key] = $('<select class="form-control"></select>').attr("id", `source-data-${key}`)
        .prop("disabled", true).appendTo(group).on("change", () => {
          this.resetAfter(index); this.search.val(""); this.load();
        });
      this.setOptions(key, []);
    });
    this.recordFilterArea = $('<div class="source-record-filters"></div>').hide().appendTo(filterBody);
    const filterField = $('<div class="source-filter-field"></div>').appendTo(this.recordFilterArea);
    $('<label for="source-data-filter-field"></label>').text(__("Filter by")).appendTo(filterField);
    this.filterField = $('<select id="source-data-filter-field" class="form-control"></select>').appendTo(filterField);
    const filterValue = $('<div class="source-filter-value"></div>').appendTo(this.recordFilterArea);
    $('<label for="source-data-filter-value"></label>').text(__("Exact value")).appendTo(filterValue);
    this.filterValue = $('<input id="source-data-filter-value" type="text" class="form-control">').attr("aria-label", __("Filter value")).appendTo(filterValue);
    const apply = () => {
      this.exactFilters = this.filterField.val() ? {[this.filterField.val()]: this.filterValue.val()} : {};
      this.offset = 0; this.load();
    };
    this.filterValue.on("keydown", event => { if (event.key === "Enter") { event.preventDefault(); apply(); } });
    $('<button type="button" class="btn btn-default"></button>').text(__("Apply filter")).appendTo(this.recordFilterArea).on("click", apply);
    this.filterNote = $('<p class="source-filter-note"></p>').text(__("Choose a brand to discover its models, years and specifications.")).appendTo(filterBody);
    const toolbar = $('<div class="source-toolbar"></div>').appendTo(this.root);
    const search = $('<div class="source-search-wrap"></div>').append(this.icon("search")).appendTo(toolbar);
    $('<label for="source-data-search" class="sr-only"></label>').text(__("Search results")).appendTo(search);
    this.search = $('<input type="search" id="source-data-search" class="form-control">')
      .attr("placeholder", __("Search brands and models…")).appendTo(search).on("input", () => {
        this.localOffset = 0; this.selected.clear(); this.render(); this.updateAddActions();
      });
    this.searchClear = $('<button type="button" class="source-search-clear"></button>').attr("aria-label", __("Clear search"))
      .append(this.icon("close")).hide().appendTo(search).on("click", () => {
        this.search.val(""); this.localOffset = 0; this.selected.clear(); this.render(); this.updateAddActions(); this.search.trigger("focus");
      });
    const actions = $('<div class="source-actions"></div>').appendTo(toolbar);
    this.addSingle = $('<button type="button" class="btn source-add"></button>').text(__("Add Vehicle"))
      .prop("disabled", true).hide().appendTo(actions).on("click", () => this.showAddDialog(false));
    this.addBulk = $('<button type="button" class="btn btn-default"></button>').text(__("Bulk Add Vehicles"))
      .prop("disabled", true).hide().appendTo(actions).on("click", () => this.showAddDialog(true));
    $('<button type="button" class="btn source-reload"></button>').attr({"aria-label": __("Reload"), "title": __("Reload")}).append(this.icon("refresh"))
      .append($('<span></span>').text(__("Reload"))).appendTo(actions).on("click", () => this.load());
    const results = $('<section class="source-results" aria-labelledby="source-results-heading"></section>').appendTo(this.root);
    const resultsHeading = $('<div class="source-results-heading"></div>').appendTo(results);
    const headingGroup = $('<div class="source-heading-group"></div>').appendTo(resultsHeading);
    this.heading = $('<h3 id="source-results-heading" tabindex="-1"></h3>').appendTo(headingGroup);
    this.resultCount = $('<span class="source-count"></span>').appendTo(headingGroup);
    this.manageRecords = $('<button type="button" class="source-quiet-button"></button>').text(__("Manage vehicle records"))
      .appendTo(resultsHeading).on("click", () => this.changeCategory("vehicle_records"));
    this.breadcrumb = $('<nav class="source-breadcrumb"></nav>').attr("aria-label", __("Vehicle exploration")).appendTo(results);
    this.selectionBar = $('<div class="source-selection"></div>').hide().appendTo(results);
    this.selectionCount = $('<span class="source-selection-count" role="status"></span>').appendTo(this.selectionBar);
    $('<button type="button" class="source-quiet-button"></button>').text(__("Clear selection")).appendTo(this.selectionBar).on("click", () => {
      this.selected.clear(); this.render(); this.updateAddActions(); this.tableArea.find('input[type="checkbox"]').first().trigger("focus");
    });
    this.deleteSelected = $('<button type="button" class="btn btn-danger"></button>').text(__("Delete selected"))
      .appendTo(this.selectionBar).on("click", () => this.reviewDeletions([...this.selected.values()]));
    this.loadingArea = $('<div class="source-skeleton" aria-hidden="true"></div>').hide().appendTo(results);
    for (let i = 0; i < 8; i++) $('<span></span>').appendTo(this.loadingArea);
    this.tableArea = $('<div class="source-table"></div>').appendTo(results);
    this.empty = $('<div class="source-empty"></div>').hide().appendTo(results);
    const footer = $('<div class="source-footer"></div>').appendTo(results);
    this.range = $('<span></span>').appendTo(footer);
    const navigation = $('<div class="source-pagination"></div>').appendTo(footer);
    this.previous = $('<button type="button" class="btn btn-default"></button>').text(__("Previous"))
      .appendTo(navigation).on("click", () => this.move(-1));
    this.next = $('<button type="button" class="btn btn-default"></button>').text(__("Next"))
      .appendTo(navigation).on("click", () => this.move(1));
    const meta = $('<div class="source-meta"></div>').appendTo(this.root);
    this.status = $('<p class="source-status" role="status" aria-live="polite"></p>').appendTo(meta);
    $('<p class="source-cache-note"></p>').text(__("Search covers loaded results · Cached for up to a minute")).appendTo(meta);
  }

  icon(name) {
    const paths = {
      car: 'M5 17H3v-6l2-6h14l2 6v6h-2M5 17h14M5 17v3M19 17v3M3 11h18M7 14h.01M17 14h.01',
      list: 'M9 5h11M9 12h11M9 19h11M4 5h.01M4 12h.01M4 19h.01',
      wheel: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10M12 7v10M7 12h10',
      palette: 'M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-4 2 2 0 0 1 1-4h2a4 4 0 0 0 4-4c0-3-4-6-9-6M7 10h.01M10 7h.01M15 7h.01',
      search: 'M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15M16 16l5 5',
      filter: 'M4 6h16M7 12h10M10 18h4',
      refresh: 'M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M19 16A8 8 0 0 1 6 19l-2-3',
      arrow: 'M9 5l7 7-7 7',
      close: 'M6 6l12 12M18 6 6 18',
      external: 'M14 3h7v7M21 3 10 14M10 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6',
      settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M10 3h4l1 3 3 1 3 3v4l-3 1-1 3-3 3h-4l-1-3-3-1-3-3v-4l3-1 1-3z',
    };
    return `<svg class="source-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.list}"></path></svg>`;
  }

  changeCategory(value) {
    this.category = value;
    this.offset = 0;
    this.search.val("");
    this.filterArea.toggle(value === "vehicles");
    this.recordFilterArea.toggle(value !== "vehicles");
    this.filterTitle.text(value === "vehicles" ? __("Explore a vehicle") : __("Filter records"));
    this.filterNote.text(value === "vehicles" ? __("Choose a brand to discover its models, years and specifications.") : __("Exact matches search the full catalog. Text search below covers this page."));
    this.filterValue.val("");
    this.exactFilters = {};
    this.populateFilterFields();
    this.filterField.val("");
    for (const [key, button] of Object.entries(this.categoryButtons)) {
      button.toggleClass("is-active", key === value).attr("aria-pressed", key === value ? "true" : "false");
    }
    this.load();
  }

  populateFilterFields() {
    if (!this.filterField || !this.catalogs) return;
    const current = this.filterField.val();
    this.filterField.empty().append($('<option value=""></option>').text(__("All records")));
    ["id", ...Object.keys(this.catalogs[this.catalogResource()].fields)].forEach(key =>
      $('<option></option>').val(key).text(this.fieldLabel(key)).appendTo(this.filterField));
    this.filterField.val(current || "");
  }

  fieldLabel(key) {
    return Object.hasOwn(this.labels, key) ? this.labels[key] : key.replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
  }

  clearFilters() {
    this.selects.brand.val(""); this.resetAfter(0);
    this.exactFilters = {}; this.filterField.val(""); this.filterValue.val("");
    this.search.val(""); this.offset = 0; this.localOffset = 0;
    this.load();
  }

  setOptions(key, values) {
    const select = this.selects[key].empty();
    $('<option value=""></option>').text(__("Choose {0}", [this.labels[key].toLowerCase()])).appendTo(select);
    const unique = [...new Set(values.map(String))].sort((a, b) => key === "year" ? Number(b) - Number(a) : a.localeCompare(b));
    unique.forEach(value => $('<option></option>').val(value).text(value).appendTo(select));
    select.prop("disabled", !unique.length);
  }

  resetAfter(index) {
    this.filters.slice(index + 1).forEach(key => this.setOptions(key, []));
  }

  requestArgs() {
    if (this.category !== "vehicles") return {resource: this.category, filters: this.exactFilters || {}, start: this.offset};
    const filters = {};
    for (const key of this.filters) {
      const value = this.selects[key].val();
      if (!value) break;
      filters[key] = value;
    }
    return {resource: ["brands", "models", "years", "structures", "engines"][Object.keys(filters).length], filters, start: 0};
  }

  async load() {
    const sequence = ++this.sequence;
    const args = this.requestArgs();
    this.resource = args.resource;
    this.localOffset = 0;
    this.rows = [];
    if (this.selected) this.selected.clear();
    this.loading = true;
    this.result = null;
    if (this.loadingArea) this.loadingArea.show();
    if (this.apiDocumentation) this.apiDocumentation.hide();
    if (this.resultCount) this.resultCount.text("…");
    if (this.root) this.root.attr("aria-busy", "true");
    this.updateAddActions();
    this.heading.text({brands: __("Vehicle brands"), models: __("Vehicle models"), years: __("Model years"),
      structures: __("Body styles"), engines: __("Engines"), vehicle_records: __("Vehicle records"), tires: __("Tires"), colors: __("Colors")}[this.resource]);
    this.status.removeClass("text-danger").text(__("Loading source data…"));
    this.tableArea.empty().hide();
    this.empty.hide();
    this.range.text("");
    this.previous.prop("disabled", true);
    this.next.prop("disabled", true);
    try {
      const response = await frappe.call({
        method: "frappe_controller.api.source_data.browse", type: "GET", silent: true,
        args: {...args, filters: JSON.stringify(args.filters), page_length: 50},
        error() {}, // Errors are rendered inline below.
      });
      if (sequence !== this.sequence) return;
      this.result = response.message;
      if (this.apiDocumentation) this.apiDocumentation.attr("href", this.result.source + "/tires/docs").show();
      this.canAdd = Boolean(this.result.can_write);
      this.catalogs = this.result.catalogs;
      this.populateFilterFields();
      this.maxAddRows = this.result.max_add_rows || 100;
      this.rows = this.result.items;
      const field = this.lookupFields[this.resource];
      if (field) {
        this.rows = [...this.rows].sort((a, b) => field === "year" ? Number(b[field]) - Number(a[field]) : String(a[field]).localeCompare(String(b[field])));
        const key = {brands: "brand", models: "model", years: "year", structures: "structure_type"}[this.resource];
        if (key) this.setOptions(key, this.rows.map(row => row[field]));
      }
      this.loading = false;
      if (this.connectionOrigin) this.connectionOrigin.text(this.result.source.replace(/^https:\/\//, "")).attr("title", this.result.source);
      if (this.connection) this.connection.removeClass("is-offline");
      this.status.text(__("Source: {0} · Retrieved: {1}", [this.result.source, new Date(this.result.fetched_at).toLocaleString()]));
      this.render();
      if (this.focusResults) { this.heading.trigger("focus"); this.focusResults = false; }
    } catch (error) {
      if (sequence !== this.sequence) return;
      this.loading = false;
      this.rows = [];
      this.status.addClass("text-danger").text(this.errorMessage(error));
      this.showEmpty(__("We couldn’t load this catalog"), __("Check the connection and try again."), true);
      if (this.connection) this.connection.addClass("is-offline");
      if (this.connectionOrigin) this.connectionOrigin.text(__("Connection unavailable"));
      if (this.resultCount) this.resultCount.text(__("Unavailable"));
    }
    if (sequence === this.sequence) {
      if (this.loadingArea) this.loadingArea.hide();
      if (this.root) this.root.attr("aria-busy", "false");
      this.updateAddActions();
    }
  }

  updateAddActions() {
    if (!this.addSingle) return;
    const label = this.catalogs ? this.catalogs[this.catalogResource()].label : "Vehicle";
    this.addSingle.text(__("Add {0}", [label])).attr("aria-label", __("Add {0}", [label]));
    this.addBulk.text(__("Bulk Add {0}", [this.catalogResource() === "colors" ? "Colors" : this.catalogResource() === "tires" ? "Tires" : "Vehicles"]));
    for (const button of [this.addSingle, this.addBulk]) {
      button.toggle(Boolean(this.canAdd))
        .prop("disabled", Boolean(this.mutationBusy || this.loading || !this.result));
    }
    if (this.deleteSelected) this.deleteSelected.toggle(Boolean(this.canAdd && this.category !== "vehicles"))
      .text(__("Delete selected ({0})", [this.selected.size]))
      .prop("disabled", Boolean(this.mutationBusy || this.loading || !this.selected.size));
    if (this.selectionBar) this.selectionBar.toggle(Boolean(this.canAdd && this.category !== "vehicles" && this.selected.size));
    if (this.selectionCount) this.selectionCount.text(__("{0} selected on this page", [this.selected.size]));
    if (this.manageRecords) this.manageRecords.toggle(this.category === "vehicles");
    if (this.settingsLink) this.settingsLink.toggle(Boolean(this.canAdd));
  }

  catalogResource() { return ["vehicles", "vehicle_records"].includes(this.category) ? "vehicles" : this.category; }

  vehicleAutocomplete(dialog, bulk = false, source = this.result?.source) {
    const attach = fields => {
      if (fields.brand?.$input && fields.model?.$input && !fields.brand.sourceVehicleAutocomplete) {
        fields.brand.sourceVehicleAutocomplete = new frappe.pages["source-data"].VehicleAutocomplete(
          fields.brand, fields.model, error => this.errorMessage(error), source);
      }
    };
    if (!bulk) return attach(dialog.fields_dict);
    // Grid controls are created lazily, including the expanded row editor.
    dialog.$wrapper.on("focusin", '[data-fieldname="vehicles"] input', event => {
      const row = $(event.target).closest(".grid-row").data("grid_row");
      if (row) attach($(event.target).closest(".form-in-grid").length
        ? row.grid_form.fields_dict : row.on_grid_fields_dict);
    });
  }

  static parseCSV(text, allowed = ["brand", "model"], required = ["brand", "model"]) {
    if (text.length > 64 * 1024) throw new Error(__("CSV must be 64 KB or smaller."));
    text = text.replace(/^\uFEFF/, "");
    const rows = [];
    let row = [], value = "", quoted = false, closed = false;
    const cell = () => { row.push(value); value = ""; closed = false; };
    const line = () => { cell(); if (row.some(value => value.trim())) rows.push(row); row = []; };
    for (let index = 0; index < text.length; index++) {
      const char = text[index];
      if (quoted) {
        if (char === '"' && text[index + 1] === '"') { value += '"'; index++; }
        else if (char === '"') { quoted = false; closed = true; }
        else value += char;
      } else if (char === ",") cell();
      else if (char === "\n" || char === "\r") {
        if (char === "\r" && text[index + 1] === "\n") index++;
        line();
      } else if (closed) {
        if (char.trim()) throw new Error(__("Unexpected text after a quoted CSV value."));
      } else if (char === '"') {
        if (value.trim()) throw new Error(__("Invalid quote in CSV value."));
        value = ""; quoted = true;
      } else value += char;
    }
    if (quoted) throw new Error(__("Unclosed quote in CSV."));
    if (value || row.length || closed) line();
    const headers = (rows.shift() || []).map(value => value.trim());
    if (new Set(headers).size !== headers.length || headers.some(key => !allowed.includes(key)) || required.some(key => !headers.includes(key))) {
      throw new Error(__("CSV must use supported field names and include every required column."));
    }
    if (!rows.length || rows.length > 100) throw new Error(__("Supply between 1 and 100 vehicle rows."));
    return rows.map((row, index) => {
      if (row.length !== headers.length) throw new Error(__("CSV row {0} has an unexpected number of values.", [index + 2]));
      return Object.fromEntries(headers.map((key, column) => [key, row[column].trim()]));
    });
  }

  showAddDialog(bulk) {
    if (!this.canAdd || this.mutationBusy || !this.result) return;
    const resource = this.catalogResource();
    const schema = this.catalogs[resource];
    const source = this.result.source;
    const brand = resource === "vehicles" ? this.selects.brand.val() || "" : "";
    const fields = Object.entries(schema.fields).map(([key, field], index) => ({
      fieldname: key, label: this.labels[key] || key.replaceAll("_", " "),
      fieldtype: resource === "vehicles" && ["brand", "model"].includes(key) ? "Autocomplete"
        : field.type === "number" ? key === "year" ? "Int" : "Float" : "Data",
      ignore_validation: resource === "vehicles" && ["brand", "model"].includes(key),
      length: field.maxLength,
      reqd: schema.required.includes(key) ? 1 : 0, in_list_view: index < 4 ? 1 : 0,
      default: key === "brand" ? brand : undefined,
    }));
    const dialog = new frappe.ui.Dialog({
      title: bulk ? __("Bulk Add {0}", [resource]) : __("Add {0}", [schema.label]),
      size: bulk ? "large" : undefined,
      fields: bulk ? [
        {fieldname: "csv_import", fieldtype: "HTML"},
        {fieldname: "vehicles", label: __("Records"), fieldtype: "Table", in_place_edit: true,
          data: [resource === "vehicles" ? {brand, model: ""} : {}], fields},
      ] : fields,
      primary_action_label: __("Review additions"),
      primary_action: async () => {
        const values = dialog.get_values();
        if (!values) return;
        if (!bulk) Object.keys(schema.fields).forEach(key => {
          const input = dialog.fields_dict[key].$input;
          if (input && !String(input.val() || "").trim() && !schema.required.includes(key)) delete values[key];
        });
        const records = (bulk ? values.vehicles || [] : [values]).map(row => Object.fromEntries(
          Object.keys(schema.fields).filter(key => schema.required.includes(key) || (row[key] !== undefined && row[key] !== null && row[key] !== ""))
            .map(key => [key, row[key] === undefined ? "" : row[key]])
        ));
        dialog.disable_primary_action();
        try {
          const response = await frappe.call({method: "frappe_controller.api.source_data.validate_add", type: "POST",
            args: {resource, records: JSON.stringify(records), source}, silent: true, error() {}});
          dialog.hide();
          this.reviewAdditions(response.message.records, () => dialog.show(), resource, "add", source);
        } catch (error) {
          frappe.msgprint({title: __("Check rows"), message: frappe.utils.escape_html(this.errorMessage(error)), indicator: "red"});
        } finally { dialog.enable_primary_action(); }
      },
    });
    if (bulk) {
      dialog.$wrapper.addClass("controller-source-vehicle-dialog");
      const area = dialog.fields_dict.csv_import.$wrapper;
      area.append(`<style>
        .controller-source-vehicle-dialog .source-csv-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px }
        .controller-source-vehicle-dialog .source-csv-actions .btn { margin:0 }
        .controller-source-vehicle-dialog .source-csv-status:empty { display:none }
        .controller-source-vehicle-dialog [data-fieldname="vehicles"] .data-row { width:100%; min-width:0 }
        .controller-source-vehicle-dialog [data-fieldname="vehicles"] .grid-static-col[data-fieldname] {
          display:block; flex:1 1 0 !important; width:0 !important; min-width:0; max-width:none;
          position:relative; inset-inline-end:auto; text-align:start;
        }
        .controller-source-vehicle-dialog [data-fieldname="vehicles"] .grid-static-col[data-fieldname] .static-area { text-align:start }
        .controller-source-vehicle-dialog [data-fieldname="vehicles"] .grid-col-resize-handle { display:none }
        .controller-source-vehicle-dialog .editable-row .grid-static-col:has(.source-lookup-status) {
          height:auto; min-height:43px; max-height:none;
        }
        .controller-source-vehicle-dialog .data-row .source-lookup-status { padding:4px 8px; white-space:normal }
      </style>`);
      $('<p class="text-muted"></p>').text(__("Add up to {0} rows. Required CSV columns: {1}. Optional columns: {2}.",
        [this.maxAddRows, schema.required.join(", "), Object.keys(schema.fields).filter(key => !schema.required.includes(key)).join(", ") || __("None")])).appendTo(area);
      const actions = $('<div class="source-csv-actions"></div>').appendTo(area);
      const input = $('<input type="file" accept=".csv,text/csv" class="sr-only" tabindex="-1">').appendTo(area);
      $('<button type="button" class="btn btn-default"></button>').text(__("Import CSV"))
        .appendTo(actions).on("click", () => input[0].click());
      $('<button type="button" class="btn btn-default"></button>').text(__("Download template"))
        .appendTo(actions).on("click", () => frappe.tools.downloadify([Object.keys(schema.fields)], null, resource));
      const status = $('<p class="source-csv-status text-muted mt-2" role="status"></p>').appendTo(area);
      input.on("change", async () => {
        const file = input[0].files[0];
        if (!file) return;
        try {
          if (file.size > 64 * 1024) throw new Error(__("CSV must be 64 KB or smaller."));
          const records = this.constructor.parseCSV(await file.text(), Object.keys(schema.fields), schema.required);
          dialog.fields_dict.vehicles.df.data = records;
          dialog.fields_dict.vehicles.grid.refresh();
          status.removeClass("text-danger").text(__("Imported {0} rows for review.", [records.length]));
        } catch (error) { status.addClass("text-danger").text(error.message); }
        input.val("");
      });
    }
    if (resource === "vehicles") this.vehicleAutocomplete(dialog, bulk, source);
    dialog.show();
  }

  async reviewDeletions(records) {
    if (!records.length || !this.canAdd || this.mutationBusy) return;
    const resource = this.catalogResource();
    const source = this.result.source;
    const targets = records.map(row => ({id: row.id, snapshot: this.result.snapshots[String(row.id)]}));
    try {
      await frappe.call({method: "frappe_controller.api.source_data.validate_delete", type: "POST", silent: true,
        args: {resource, records: JSON.stringify(targets), source}, error() {}});
      this.reviewAdditions(records.map((row, index) => ({id: row.id, label: this.recordLabel(row), snapshot: targets[index].snapshot})),
        () => {}, resource, "delete", source);
    } catch (error) {
      frappe.msgprint({message: frappe.utils.escape_html(this.errorMessage(error)), indicator: "red"});
    }
  }

  recordLabel(record) {
    return record.label || [record.tire_name || record.colorNameEN || record.brand, record.model, record.year]
      .filter(value => value !== undefined && value !== null && value !== "").join(" · ") + (record.id ? " · ID " + record.id : "");
  }

  static editChanges(original, values, schema) {
    const changes = {};
    for (const [key, field] of Object.entries(schema.fields)) {
      if (!Object.prototype.hasOwnProperty.call(values, key)) continue;
      const before = original[key] == null ? "" : String(original[key]).trim();
      const after = values[key] == null ? "" : String(values[key]).trim();
      if (before === after) continue;
      if (field.type === "number" && before !== "" && after !== "" && Number(before) === Number(after)) continue;
      changes[key] = after === "" && field.nullable ? null : after;
    }
    return changes;
  }

  async showEditDialog(row) {
    if (!this.canAdd || this.mutationBusy || this.editLoading) return;
    const resource = this.catalogResource();
    this.editLoading = true;
    try {
      const response = await frappe.call({method: "frappe_controller.api.source_data.read", type: "GET", silent: true,
        args: {resource, record_id: row.id, source: this.result.source}, error() {}});
      this.showRecordEditor(resource, response.message.record, response.message.snapshot, response.message.source);
    } catch (error) {
      frappe.msgprint({message: frappe.utils.escape_html(this.errorMessage(error)), indicator: "red"});
    } finally { this.editLoading = false; }
  }

  showRecordEditor(resource, original, digest, source = this.result?.source) {
    const schema = this.catalogs[resource];
    const dialog = new frappe.ui.Dialog({
      title: __("Edit {0} · ID {1}", [schema.label, original.id]),
      fields: [{fieldname: "record_id", label: __("Record ID"), fieldtype: "Int", read_only: 1, default: original.id},
        ...Object.entries(schema.fields).map(([key, field]) => ({
          fieldname: key, label: this.labels[key] || key.replaceAll("_", " "),
          fieldtype: resource === "vehicles" && ["brand", "model"].includes(key) ? "Autocomplete"
            : field.type === "number" ? key === "year" ? "Int" : "Float" : "Data",
          ignore_validation: resource === "vehicles" && ["brand", "model"].includes(key),
          length: field.maxLength,
          reqd: schema.required.includes(key) ? 1 : 0,
          default: original[key] == null ? undefined : original[key],
          description: field.nullable ? __("Leave empty to clear this value.") : undefined,
        }))],
      primary_action_label: __("Review changes"),
      primary_action: async () => {
        const parsed = dialog.get_values();
        if (!parsed) return;
        const values = Object.fromEntries(Object.entries(schema.fields).map(([key, field]) => {
          const raw = dialog.fields_dict[key].$input.val();
          return [key, String(raw || "").trim() && field.type === "number" && Number.isFinite(parsed[key]) ? parsed[key] : raw];
        }));
        const changes = this.constructor.editChanges(original, values, schema);
        if (!Object.keys(changes).length) {
          frappe.show_alert({message: __("No changes detected."), indicator: "blue"}); return;
        }
        dialog.disable_primary_action();
        try {
          const response = await frappe.call({method: "frappe_controller.api.source_data.validate_edit", type: "POST", silent: true,
            args: {resource, record_id: original.id, changes: JSON.stringify(changes), snapshot_hash: digest, source}, error() {}});
          dialog.hide();
          this.reviewEdit(resource, original, response.message.changes, digest, () => dialog.show(), source);
        } catch (error) {
          frappe.msgprint({message: frappe.utils.escape_html(this.errorMessage(error)), indicator: "red"});
        } finally { dialog.enable_primary_action(); }
      },
    });
    dialog.show();
    for (const [key, field] of Object.entries(schema.fields)) {
      if (field.nullable && original[key] == null) dialog.fields_dict[key].$input.val("");
    }
    if (resource === "vehicles") this.vehicleAutocomplete(dialog, false, source);
  }

  reviewEdit(resource, original, changes, digest, back, source = this.result?.source) {
    const review = new frappe.ui.Dialog({title: __("Review changes · ID {0}", [original.id]), size: "large",
      fields: [{fieldname: "review", fieldtype: "HTML"}], primary_action_label: __("Save changes"),
      primary_action: () => {
        if (this.mutationBusy) return;
        review.disable_primary_action(); review.hide();
        this.runAdditions([{id: original.id, label: this.recordLabel(original), snapshot: digest, changes}], resource, "edit", source);
      },
      secondary_action_label: __("Back"), secondary_action: () => { review.hide(); back(); },
    });
    const area = review.fields_dict.review.$wrapper;
    $('<p class="text-muted"></p>').text(__("Source: {0}", [source])).appendTo(area);
    $('<p></p>').text(__("Save only these changed fields to the source catalog. A record changed by someone else must be reviewed again.")).appendTo(area);
    const table = $('<table class="table"></table>').appendTo($('<div style="overflow:auto"></div>').appendTo(area));
    const head = $('<tr></tr>').appendTo($('<thead></thead>').appendTo(table));
    [__("Field"), __("Before"), __("After")].forEach(label => $('<th scope="col"></th>').text(label).appendTo(head));
    const body = $('<tbody></tbody>').appendTo(table);
    for (const [key, value] of Object.entries(changes)) {
      const row = $('<tr></tr>').appendTo(body);
      [this.labels[key] || key, original[key] == null || original[key] === "" ? __("(empty)") : String(original[key]),
        value == null || value === "" ? __("(empty)") : String(value)]
        .forEach(text => $('<td></td>').text(text).appendTo(row));
    }
    review.show();
  }

  reviewAdditions(records, back, resource = "vehicles", action = "add", source = this.result?.source) {
    const deleting = action === "delete";
    const review = new frappe.ui.Dialog({title: deleting ? __("Review {0} deletions", [records.length]) : __("Review {0} additions", [records.length]), size: "large",
      fields: [{fieldname: "review", fieldtype: "HTML"}, ...(deleting ? [{fieldname: "confirm_delete", label: __("Type DELETE to confirm"), fieldtype: "Data", reqd: 1}] : [])],
      primary_action_label: deleting ? __("Delete {0} records", [records.length]) : __("Add {0} records", [records.length]),
      primary_action: () => {
        if (this.mutationBusy) return;
        if (deleting && review.get_value("confirm_delete") !== "DELETE") {
          frappe.msgprint(__("Type DELETE to confirm these record IDs.")); return;
        }
        review.disable_primary_action(); review.hide();
        this.runAdditions(records, resource, action, source);
      },
      secondary_action_label: __("Back"), secondary_action: () => { review.hide(); back(); },
    });
    const area = review.fields_dict.review.$wrapper;
    $('<p class="text-muted"></p>').text(__("Source: {0}", [source])).appendTo(area);
    $('<p></p>').text(deleting ? __("Delete only these record IDs from the source catalog. Linked records may prevent deletion. Changed records must be reviewed again.") :
      __("Add these records to the source catalog. Matching records will be skipped.")).appendTo(area);
    $('<p class="text-muted"></p>').text(__("Rows are processed individually. Completed additions remain if a later row fails or you stop the batch.")).appendTo(area);
    const container = $('<div style="max-height:340px;overflow:auto"></div>').appendTo(area);
    const table = $('<table class="table"></table>').appendTo(container);
    const head = $('<tr></tr>').appendTo($('<thead></thead>').appendTo(table));
    const columns = [...new Set(records.flatMap(record => Object.keys(record)))].filter(key => key !== "snapshot");
    columns.forEach(label => $('<th scope="col"></th>').text(this.labels[label] || label).appendTo(head));
    const body = $('<tbody></tbody>').appendTo(table);
    records.forEach(record => {
      const row = $('<tr></tr>').appendTo(body);
      columns.forEach(key => $('<td></td>').text(record[key] == null ? "" : String(record[key])).appendTo(row));
    });
    review.show();
  }

  async runAdditions(records, resource = "vehicles", action = "add", source = this.result?.source) {
    if (this.mutationBusy) return;
    this.mutationBusy = true;
    this.stopRequested = false;
    this.render();
    this.updateAddActions();
    if (this.resultsArea) this.resultsArea.remove();
    this.resultsArea = $('<section class="source-mutation-results mt-4"></section>').attr("aria-label", __("Change results")).appendTo(this.root);
    $('<h4></h4>').text(action === "edit" ? __("Edit results") : action === "delete" ? __("Deletion results") : __("Addition results")).appendTo(this.resultsArea);
    const progress = $('<p role="status" aria-live="polite"></p>').appendTo(this.resultsArea);
    const stop = $('<button type="button" class="btn btn-default mb-3"></button>').text(__("Stop remaining rows"))
      .appendTo(this.resultsArea).on("click", () => { this.stopRequested = true; stop.prop("disabled", true); });
    const table = $('<table class="table"></table>').appendTo($('<div class="source-table"></div>').appendTo(this.resultsArea));
    const head = $('<tr></tr>').appendTo($('<thead></thead>').appendTo(table));
    [__("Record"), __("Result"), __("Details")].forEach(label => $('<th scope="col"></th>').text(label).appendTo(head));
    const body = $('<tbody></tbody>').appendTo(table);
    const items = records.map(record => {
      const row = $('<tr></tr>').appendTo(body);
      $('<td></td>').text(this.recordLabel(record)).appendTo(row);
      return {record, request_id: crypto.randomUUID(), state: "not_sent",
        status: $('<td></td>').text(__("Waiting")).appendTo(row), message: $('<td></td>').appendTo(row)};
    });
    this.lastAdditions = items;
    let processed = 0;
    try {
      for (const item of items) {
        if (this.stopRequested) break;
        item.status.text(__("Sending…"));
        try {
          const args = action === "add" ? {resource, record: JSON.stringify(item.record), request_id: item.request_id} :
            {resource, record_id: item.record.id, snapshot_hash: item.record.snapshot, request_id: item.request_id,
              ...(action === "edit" ? {changes: JSON.stringify(item.record.changes)} : {})};
          const method = {add: "add", delete: "remove", edit: "edit"}[action];
          args.source = source;
          const response = await frappe.call({method: "frappe_controller.api.source_data." + method, type: "POST", silent: true, args, error() {}});
          const result = response.message;
          item.state = ["added", "existing", "deleted", "missing", "updated", "unchanged", "failed", "uncertain"].includes(result.state) ? result.state : "uncertain";
          item.message.text(result.message);
        } catch (error) {
          item.state = ["ValidationError", "PermissionError"].includes(error && error.responseJSON && error.responseJSON.exc_type) ? "failed" : "uncertain";
          item.message.text(item.state === "failed" ? this.errorMessage(error) : __("No result received. Check the source before trying again."));
        }
        item.status.text({added: __("Added"), existing: __("Already exists"), deleted: __("Deleted"), missing: __("Already absent"), updated: __("Updated"), unchanged: __("Unchanged"), failed: __("Failed"), uncertain: __("Check source")}[item.state]);
        processed++;
        progress.text(__("Processed {0} of {1} rows.", [processed, items.length]));
        if (item.state === "uncertain") this.stopRequested = true;
      }
      items.filter(item => item.state === "not_sent").forEach(item => { item.status.text(__("Not sent")); });
      const count = state => items.filter(item => item.state === state).length;
      const summary = {edit: "{0} updated · {1} unchanged · {2} failed · {3} need checking · {4} not sent",
        delete: "{0} deleted · {1} already absent · {2} failed · {3} need checking · {4} not sent",
        add: "{0} added · {1} already exist · {2} failed · {3} need checking · {4} not sent"}[action];
      progress.text(__(summary, [count({edit: "updated", delete: "deleted", add: "added"}[action]),
        count({edit: "unchanged", delete: "missing", add: "existing"}[action]), count("failed"), count("uncertain"), count("not_sent")]));
    } finally {
      this.mutationBusy = false;
      this.render();
      stop.hide();
      this.updateAddActions();
    }
    if (items.some(item => ["added", "deleted", "missing", "updated", "uncertain"].includes(item.state))) {
      if (this.category === "vehicles") {
        this.resetAfter(-1);
        this.selects.brand.val("");
        this.search.val("");
      }
      await this.load();
    }
  }

  errorMessage(error) {
    const response = error && error.responseJSON;
    if (response && ["ValidationError", "PermissionError"].includes(response.exc_type)) {
      try {
        const messages = JSON.parse(response._server_messages || "[]");
        const value = JSON.parse(messages[messages.length - 1]);
        if (typeof value.message === "string") return value.message;
      } catch (_) { /* Unexpected errors use a generic message. */ }
    }
    return __("Source data is unavailable. Please try again later.");
  }

  showEmpty(title, description, retry = false) {
    this.empty.empty().append(this.icon(retry ? "refresh" : "search"));
    $('<strong></strong>').text(title).appendTo(this.empty);
    $('<div></div>').text(description).appendTo(this.empty);
    if (retry) $('<button type="button" class="btn btn-default"></button>').text(__("Try again")).appendTo(this.empty).on("click", () => this.load());
    else if (this.search.val()) $('<button type="button" class="btn btn-default"></button>').text(__("Reset search")).appendTo(this.empty).on("click", () => {
      this.search.val(""); this.localOffset = 0; this.selected.clear(); this.render(); this.updateAddActions(); this.search.trigger("focus");
    });
    this.empty.show();
  }

  renderBreadcrumbs() {
    if (!this.breadcrumb) return;
    this.breadcrumb.empty();
    if (this.category !== "vehicles" || !this.selects.brand.val()) return;
    $('<button type="button"></button>').text(__("All brands")).appendTo(this.breadcrumb).on("click", () => {
      this.focusResults = true; this.clearFilters();
    });
    this.filters.forEach((key, index) => {
      const value = this.selects[key].val();
      if (!value) return;
      this.breadcrumb.append(this.icon("arrow"));
      $('<button type="button"></button>').text(value).attr("aria-label", __("Back to {0}", [value]))
        .appendTo(this.breadcrumb).on("click", () => {
          this.resetAfter(index); this.search.val(""); this.focusResults = true; this.load();
        });
    });
  }

  renderLookupCards(rows) {
    const field = this.lookupFields[this.resource];
    const selectKey = {brands: "brand", models: "model", years: "year", structures: "structure_type"}[this.resource];
    const grid = $('<div class="source-lookup-grid"></div>').appendTo(this.tableArea);
    rows.forEach(row => {
      const value = String(row[field]);
      const card = $(selectKey ? '<button type="button" class="source-lookup-card"></button>' : '<div class="source-lookup-card is-static"></div>').appendTo(grid);
      $('<span class="source-monogram" aria-hidden="true"></span>').text(Array.from(value).slice(0, 2).join("")).appendTo(card);
      $('<span class="source-value"></span>').text(value).appendTo(card);
      if (selectKey) card.append(this.icon("arrow")).on("click", () => {
        this.focusResults = true;
        this.selects[selectKey].val(value).trigger("change");
      });
    });
  }

  recordColumns(rows) {
    const layouts = {
      vehicle_records: ["id", "brand", "model", "year", "structureType", "engine"],
      tires: ["id", "tire_name", "brand", "tire_size", "tire_type", "section_width", "rim_diameter"],
      colors: ["id", "colorNameEN", "colorNameAR"],
    };
    const keys = new Set(rows.flatMap(row => Object.keys(row)));
    return (layouts[this.resource] || [...keys]).filter(key => keys.has(key));
  }

  renderRecordTable(rows) {
    const columns = this.recordColumns(rows);
    const primary = {vehicle_records: "brand", tires: "tire_name", colors: "colorNameEN"}[this.resource];
    const table = $('<table class="source-records-table"></table>').appendTo(this.tableArea);
    $('<caption class="sr-only"></caption>').text(this.heading.text()).appendTo(table);
    const header = $('<tr></tr>').appendTo($('<thead></thead>').appendTo(table));
    const canWrite = this.canAdd && this.result.snapshots;
    const redrawSelection = event => {
      const label = event.target.getAttribute("aria-label");
      this.render(); this.updateAddActions();
      // Keep keyboard users on the same checkbox after the row redraw.
      this.tableArea.find('input[type="checkbox"]').filter((_, input) => input.getAttribute("aria-label") === label).trigger("focus");
    };
    if (canWrite) {
      const cell = $('<th class="source-row-check" scope="col"></th>').appendTo(header);
      $('<input type="checkbox">').attr("aria-label", __("Select all visible records"))
        .prop("checked", rows.length > 0 && rows.every(row => this.selected.has(row.id)))
        .prop("disabled", Boolean(this.mutationBusy)).appendTo(cell).on("change", event => {
          rows.forEach(row => { if (this.result.snapshots[String(row.id)]) {
            if (event.target.checked) this.selected.set(row.id, row); else this.selected.delete(row.id);
          }});
          redrawSelection(event);
        });
    }
    columns.forEach(key => $('<th scope="col"></th>').text(this.fieldLabel(key)).appendTo(header));
    if (canWrite) $('<th class="source-row-actions" scope="col"></th>').text(__("Actions")).appendTo(header);
    const body = $('<tbody></tbody>').appendTo(table);
    const colorMap = {black: "#25292f", white: "#ffffff", gray: "#91979f", grey: "#91979f", silver: "#c1c7cf", red: "#c94a4a", blue: "#4677bd", green: "#4e8768", yellow: "#dfc25a", orange: "#d99147", brown: "#876654", beige: "#d4c5a8", purple: "#8865aa", pink: "#d793b5", navy: "#344968"};
    rows.forEach(row => {
      const tr = $('<tr></tr>').toggleClass("is-selected", this.selected.has(row.id)).appendTo(body);
      if (canWrite) {
        const cell = $('<td class="source-row-check"></td>').appendTo(tr);
        $('<input type="checkbox">').attr("aria-label", __("Select record {0}", [row.id]))
          .prop("checked", this.selected.has(row.id)).prop("disabled", Boolean(this.mutationBusy || !this.result.snapshots[String(row.id)]))
          .appendTo(cell).on("change", event => {
            if (event.target.checked) this.selected.set(row.id, row); else this.selected.delete(row.id);
            redrawSelection(event);
          });
      }
      columns.forEach(key => {
        const cell = $('<td></td>').attr("data-label", this.fieldLabel(key))
          .toggleClass("source-row-id", key === "id").toggleClass("source-primary", key === primary).appendTo(tr);
        const value = row[key] == null || row[key] === "" ? "—" : typeof row[key] === "object" ? JSON.stringify(row[key]) : String(row[key]);
        if (key === primary) {
          const details = $('<button type="button" class="source-detail-link"></button>')
            .attr("aria-label", __("View details for {0}", [this.recordLabel(row)]))
            .attr("title", __("View record details"))
            .appendTo(cell).on("click", () => this.showDetails(row));
          if (this.resource === "colors") {
            const swatch = $('<span class="source-color-swatch" aria-hidden="true"></span>').appendTo(details);
            const name = String(row.colorNameEN || "").trim().toLowerCase();
            const color = Object.hasOwn(colorMap, name) ? colorMap[name] : null;
            if (color) swatch.css("--swatch", color);
          }
          $('<span class="source-record-title"></span>').text(value).appendTo(details);
          details.append(this.icon("arrow"));
        } else cell.text(value);
      });
      if (canWrite) {
        const actions = $('<td class="source-row-actions"></td>').appendTo(tr);
        const edit = $('<button type="button" class="btn source-edit"></button>').text(__("Edit"))
          .attr("aria-label", __("Edit ID {0}", [row.id])).prop("disabled", Boolean(this.mutationBusy))
          .appendTo(actions).on("click", () => this.showEditDialog(row));
        $('<span class="sr-only"></span>').text(" ID " + row.id).appendTo(edit);
        const remove = $('<button type="button" class="btn source-delete"></button>').text(__("Delete"))
          .attr("aria-label", __("Delete ID {0}", [row.id])).prop("disabled", Boolean(this.mutationBusy || !this.result.snapshots[String(row.id)]))
          .appendTo(actions).on("click", () => this.reviewDeletions([row]));
        $('<span class="sr-only"></span>').text(" ID " + row.id).appendTo(remove);
      }
    });
  }

  showDetails(record) {
    const dialog = new frappe.ui.Dialog({title: __("Record details"), size: "large",
      fields: [{fieldname: "details", fieldtype: "HTML"}],
      primary_action_label: __("Close"), primary_action: () => dialog.hide(),
    });
    const area = dialog.fields_dict.details.$wrapper;
    $('<h4></h4>').text(this.recordLabel(record)).appendTo(area);
    $('<p class="text-muted small"></p>').text(__("Source: {0}", [this.result.source])).appendTo(area);
    const table = $('<table class="table"></table>').appendTo($('<div style="max-height:60vh;overflow:auto"></div>').appendTo(area));
    const body = $('<tbody></tbody>').appendTo(table);
    Object.entries(record).forEach(([key, value]) => {
      const row = $('<tr></tr>').appendTo(body);
      $('<th scope="row" style="width:35%"></th>').text(this.fieldLabel(key)).appendTo(row);
      $('<td style="overflow-wrap:anywhere;white-space:pre-wrap"></td>')
        .text(value == null || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value)).appendTo(row);
    });
    dialog.show();
  }

  render() {
    if (this.loading || !this.result) return;
    const query = String(this.search.val() || "").trim().toLocaleLowerCase();
    const matches = this.rows.filter(row => Object.values(row).some(value => value != null &&
      (typeof value === "object" ? JSON.stringify(value) : String(value)).toLocaleLowerCase().includes(query)));
    const lookup = Boolean(this.lookupFields[this.resource]);
    const rows = lookup ? matches.slice(this.localOffset, this.localOffset + 50) : matches;
    this.tableArea.empty().toggleClass("is-lookups", lookup).toggle(Boolean(rows.length));
    this.empty.hide();
    if (!rows.length) this.showEmpty(this.rows.length ? __("No matching results") : __("No records yet"),
      this.rows.length ? __("Try a different search or reset the filters.") :
        this.canAdd ? __("This view is empty. Try another filter or add a record.") : __("Try another filter or reload the catalog."));
    if (rows.length) {
      if (lookup) this.renderLookupCards(rows); else this.renderRecordTable(rows);
    }
    const format = number => Number(number).toLocaleString();
    const total = this.result.total;
    if (this.resultCount) this.resultCount.text(total == null ? __("{0} loaded", [format(this.rows.length)]) : __("{0} total", [format(total)]));
    if (this.searchClear) this.searchClear.toggle(Boolean(query));
    this.search.attr("placeholder", lookup ? __("Search all loaded {0}…", [this.resource]) : __("Search this page…"));
    this.renderBreadcrumbs();
    const start = lookup ? this.localOffset : this.offset;
    this.range.text((query ? __("{0} matches", [format(matches.length)]) : __("Showing {0}–{1}", [format(rows.length ? start + 1 : 0), format(start + rows.length)])) +
      (total == null ? "" : __(" · {0} total", [format(total)])));
    this.previous.prop("disabled", lookup ? this.localOffset === 0 : this.offset === 0);
    this.next.prop("disabled", lookup ? this.localOffset + 50 >= matches.length : !this.result.has_more);
  }

  move(direction) {
    if (this.category === "vehicles") {
      this.localOffset = Math.max(0, this.localOffset + direction * 50);
      this.render();
    } else {
      this.offset = Math.max(0, this.offset + direction * 50);
      this.load();
    }
  }
};

// Suggestions are optional: new catalog values retain the existing free-text
// contract. All reads use the authenticated, bounded Controller API facade.
frappe.pages["source-data"].VehicleAutocomplete = class VehicleAutocomplete {
  constructor(brand, model, errorMessage, source) {
    this.brand = brand;
    this.model = model;
    this.errorMessage = errorMessage;
    this.source = source;
    this.sequence = 0;
    this.currentBrand = String(brand.$input.val() || "").trim();
    this.states = {};
    for (const [key, control] of Object.entries({brand, model})) {
      const id = `source-vehicle-${key}-${crypto.randomUUID()}`;
      const described = control.$input.attr("aria-describedby") || "";
      control.$input.attr({"aria-label": __(key === "brand" ? "Brand" : "Model"),
        "aria-describedby": `${described} ${id}`.trim(), maxlength: control.df.length || 100});
      const status = $('<p class="text-muted small mb-1 source-lookup-status" role="status" aria-live="polite"></p>')
        .attr("id", id).appendTo(control.$wrapper);
      this.states[key] = {control, status, values: [], state: "waiting"};
      // Frappe's default suggestion template accepts HTML. Catalog text must
      // stay text even if an upstream value contains markup.
      control.awesomplete.item = item => $('<li aria-selected="false"></li>')
        .text(item.value === control.sourceTypedValue ? __("Use “{0}” (new value)", [item.value]) : item.label).get(0);
      control.$input.on("input.source-vehicle", () => this.renderState(key));
      control.$input.on("focus.source-vehicle", () => {
        // Another grid editor can refresh these controls from the same row.
        // Treat that as record loading and preserve its current model.
        const brand = String(this.brand.$input.val() || "").trim();
        if (brand !== this.currentBrand) {
          this.currentBrand = brand;
          clearTimeout(this.timer);
          const sequence = ++this.sequence;
          this.setState("model", brand ? "loading" : "waiting");
          if (brand) this.loadModels(brand, sequence);
        }
        if (this.states[key].state === "error") {
          if (key === "brand") this.loadBrands();
          else if (this.currentBrand) this.loadModels(this.currentBrand, ++this.sequence);
        }
        this.renderState(key);
      });
    }
    brand.$input.on("input.source-vehicle change.source-vehicle awesomplete-selectcomplete.source-vehicle", () => this.brandChanged());
    this.loadBrands();
    // Initial suggestions must never clear an existing saved model.
    if (this.currentBrand) this.loadModels(this.currentBrand, ++this.sequence);
    else this.renderState("model");
  }

  setState(key, state, values = [], error = "") {
    Object.assign(this.states[key], {state, values, error});
    this.renderState(key);
  }

  renderState(key) {
    const {control, status, state, values, error} = this.states[key];
    const typed = String(control.$input.val() || "").trim();
    const term = typed.toLocaleLowerCase();
    // Keep the option's value identical to the typed text; its explanatory
    // label is presentation only and must never enter the saved record.
    control.sourceTypedValue = typed && !values.includes(typed) ? typed : null;
    control.set_data(control.sourceTypedValue ? [typed, ...values] : values);
    if (!typed && state !== "ready") control.awesomplete.close();
    let message;
    if (state === "loading") message = __("Loading suggestions…");
    else if (state === "error") message = error + " " + __("Focus this field to retry. You can still enter a value.");
    else if (state === "waiting") message = __("Enter a brand to load model suggestions.");
    else if (!values.length || !values.some(value => value.toLocaleLowerCase().includes(term))) {
      message = __("No matching suggestions. You can enter a new value.");
    } else message = __("Type to search; use arrow keys and Enter to select. You can also enter a new value.");
    control.$input.attr("aria-busy", state === "loading" ? "true" : "false");
    status.toggleClass("text-danger", state === "error").text(message);
  }

  async suggestions(resource, filters, key) {
    const response = await frappe.call({method: "frappe_controller.api.source_data.browse", type: "GET", silent: true,
      args: {resource, filters: JSON.stringify(filters), start: 0, page_length: 100, source: this.source}, error() {}});
    const items = response.message?.items;
    if (!Array.isArray(items) || items.some(row => typeof row[key] !== "string")) throw new Error("Invalid lookup response");
    return [...new Set(items.map(row => row[key]).filter(value => value.trim()))].sort((a, b) => a.localeCompare(b));
  }

  async loadBrands() {
    this.setState("brand", "loading");
    try {
      this.setState("brand", "ready", await this.suggestions("brands", {}, "brand"));
    } catch (error) { this.setState("brand", "error", [], this.errorMessage(error)); }
  }

  brandChanged() {
    const brand = String(this.brand.$input.val() || "").trim();
    if (brand === this.currentBrand) return;
    this.currentBrand = brand;
    const sequence = ++this.sequence;
    clearTimeout(this.timer);
    // Clear the displayed value and underlying dialog/grid value immediately,
    // before the next lookup starts or a user can review the form.
    this.model.$input.val("");
    this.model.set_value("");
    this.setState("model", brand ? "loading" : "waiting");
    if (brand) this.timer = setTimeout(() => this.loadModels(brand, sequence), 200);
  }

  async loadModels(brand, sequence) {
    this.setState("model", "loading");
    try {
      const values = await this.suggestions("models", {brand}, "model");
      if (sequence === this.sequence && brand === this.currentBrand) this.setState("model", "ready", values);
    } catch (error) {
      if (sequence === this.sequence && brand === this.currentBrand) this.setState("model", "error", [], this.errorMessage(error));
    }
  }
};
