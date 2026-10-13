"""AI Sourcing Délifrance — interface Streamlit.

Ce fichier ne contient que l'interface. La logique est dans ``sourcing/`` (index, recherche,
scoring, Internet) et ``cloudinary_pdf_tools.py`` (stockage des catalogues PDF).
Aucun fournisseur n'est connu à l'avance : la recherche Internet s'adapte à la demande.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

from cloudinary_pdf_tools import (
    CloudinaryClient, RemotePdf, UploadJob, job_summary, prepare_upload, run_upload,
)
import sourcing_pdf_render as pdf_render
from sourcing_cache import BoundedTTLCache
from sourcing_errors import SourcingError
from sourcing_indexer import plan_sync, sync_index
from sourcing_internal_search import InternalHit, ScoringConfig, SearchFilters, search_internal
from sourcing_manufacturers import load_verified_manufacturers
from sourcing_models import NOT_FOUND_PUBLIC, SupplierCandidate
from sourcing_settings import Settings
from sourcing_shortlist import DECISIONS, row_from_candidate, row_from_internal, to_csv_bytes
from sourcing_store import IndexStore
from sourcing_suppliers import SupplierDirectory
from sourcing_textutils import QueryPlan, build_query_plan
from sourcing_web_search import TavilyClient, WebSearchOptions, WebSearchReport, WebSourcing

STATUS_ICONS = {"fabricant confirmé": "🟢", "fabricant probable": "🟡", "à confirmer": "🟠", "non trouvé": "⚪"}
STATUSES = ["fabricant confirmé", "fabricant probable", "à confirmer", "non trouvé"]


# ---------------------------------------------------------------------------
# Ressources partagées
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    return {k: _plain(v) for k, v in value.items()} if hasattr(value, "items") else value


def load_settings() -> Settings:
    """Secrets Streamlit (``.streamlit/secrets.toml``) puis variables d'environnement. Jamais affichés."""
    try:
        secrets = {key: _plain(st.secrets[key]) for key in st.secrets}
    except Exception:  # noqa: BLE001 - absence de fichier secrets = configuration vide, pas une erreur
        secrets = {}
    return Settings.from_mapping(secrets)


@st.cache_resource(show_spinner=False)
def get_store(db_path: str) -> IndexStore:
    return IndexStore(db_path)


@st.cache_resource(show_spinner=False)
def get_cloudinary(key: str, _settings: Settings) -> CloudinaryClient:
    """``key`` (empreinte non réversible des identifiants) invalide le cache quand les secrets changent."""
    return CloudinaryClient(_settings.cloudinary)


@st.cache_data(ttl=600, max_entries=2, show_spinner=False)
def list_remote(key: str, _client: CloudinaryClient) -> list[RemotePdf]:
    return _client.list_pdfs()


@st.cache_data(ttl=900, max_entries=64, show_spinner=False)
def cached_internal_search(
    index_version: int, query: str, suppliers: tuple[str, ...], min_w: float | None, max_w: float | None,
    only_structured: bool, tolerance: float, top_k: int, _store: IndexStore,
) -> list[InternalHit]:
    """Le cache est invalidé dès que l'index change (``index_version`` fait partie de la clé)."""
    return search_internal(
        _store, query, SearchFilters(suppliers, min_w, max_w, only_structured),
        ScoringConfig(weight_tolerance=tolerance), top_k=top_k,
    )


@st.cache_data(show_spinner=False, ttl=1800, max_entries=4)
def page_image(key: str, public_id: str, local_page: int, _client: CloudinaryClient, _store: IndexStore) -> bytes:
    """Aperçu PNG d'une page : le PDF (une partie, < 10 Mo) est téléchargé à la demande puis supprimé."""
    record = _store.get_file(public_id)
    if record is None:
        raise SourcingError("Ce PDF n'est plus dans l'index.")
    remote = RemotePdf(record.public_id, record.version or "", record.size_bytes or 0, record.etag or "",
                       record.resource_type or "raw", record.secure_url or "")
    with tempfile.TemporaryDirectory(prefix="preview_") as tmp:
        target = Path(tmp) / "page.pdf"
        _client.download_to_file(remote, target)
        return pdf_render.render_page_png(target, local_page)


def web_cache() -> BoundedTTLCache[WebSearchReport]:
    if "web_cache" not in st.session_state:
        st.session_state["web_cache"] = BoundedTTLCache(max_entries=6, ttl_seconds=3600)
    return st.session_state["web_cache"]


def known_directory(settings: Settings, store: IndexStore) -> SupplierDirectory:
    """Fournisseurs de la liste de l'acheteur + fournisseurs des catalogues déjà indexés."""
    return SupplierDirectory.from_file(settings.known_suppliers_path).with_names(store.suppliers())


# ---------------------------------------------------------------------------
# Décisions et shortlist
# ---------------------------------------------------------------------------


def set_decision(key: str, label: str, row: dict[str, str]) -> None:
    decisions: dict[str, tuple[str, dict[str, str]]] = st.session_state.setdefault("decisions", {})
    if decisions.get(key, ("", {}))[0] == label:
        decisions.pop(key, None)  # un second clic annule la décision
    else:
        decisions[key] = (label, row)


def decision_buttons(prefix: str, key: str, row: dict[str, str]) -> None:
    current = st.session_state.get("decisions", {}).get(key, ("", {}))[0]
    for column, label in zip(st.columns(len(DECISIONS)), DECISIONS):
        column.button(
            f"{label} ✓" if current == label else label, key=f"{prefix}-{key}-{label}", width="stretch",
            type="primary" if current == label else "secondary", on_click=set_decision, args=(key, label, row),
        )


# ---------------------------------------------------------------------------
# Barre latérale : configuration, synchronisation, envoi de PDF
# ---------------------------------------------------------------------------


def sidebar_status(box: Any, settings: Settings, store: IndexStore) -> None:
    """Remplit le conteneur de statut (créé en haut de la barre latérale, rempli après la synchronisation)."""
    box.header("☁️ Cloudinary")
    if settings.cloudinary_ready:
        box.write(f"✅ Cloudinary configuré · dossier « {settings.cloudinary.prefix} »")
    else:
        box.error("Cloudinary non configuré dans les Secrets Streamlit : " + ", ".join(settings.cloudinary.missing()) + ".")
    if settings.tavily_ready:
        box.write("✅ Tavily configuré")
    else:
        box.write("⚪ Tavily non configuré (TAVILY_API_KEY, clé commençant par « tvly- »)")
    stats = store.stats()
    box.metric("PDF", stats.files)
    box.metric("Passages indexés", stats.passages)
    if not store.fts_available:
        box.warning("SQLite sans FTS5 : recherche en mode de repli (plus lent).")


def run_sync(settings: Settings, store: IndexStore, client: CloudinaryClient, *, max_files: int | None) -> None:
    bar = st.sidebar.progress(0.0, text="Liste des PDF…")
    try:
        list_remote.clear()
        remotes = list_remote(settings.cloudinary.cache_key(), client)
        report = sync_index(
            store, client, remotes, max_files=max_files, directory=known_directory(settings, store),
            progress=lambda i, n, name: bar.progress(i / max(n, 1), text=f"{i}/{n} · {name.rsplit('/', 1)[-1][:40]}"),
        )
        st.sidebar.success(report.summary())
        for public_id, message in report.errors:
            st.sidebar.error(f"{public_id.rsplit('/', 1)[-1]} : {message}")
    except SourcingError as exc:
        st.sidebar.error(f"Erreur Cloudinary : {exc}")
    finally:
        bar.empty()


def sidebar_sync(settings: Settings, store: IndexStore) -> None:
    if not settings.cloudinary_ready:
        return
    client = get_cloudinary(settings.cloudinary.cache_key(), settings)
    auto = st.sidebar.checkbox("Synchroniser au démarrage", value=True, help="Seuls les PDF nouveaux ou modifiés sont retraités.")
    batch = st.sidebar.number_input("PDF par mise à jour (0 = tous)", min_value=0, max_value=200, value=0, step=5)
    if auto and not st.session_state.get("auto_synced"):
        st.session_state["auto_synced"] = True
        run_sync(settings, store, client, max_files=int(batch) or None)
    if st.sidebar.button("Synchroniser", type="primary", width="stretch"):
        run_sync(settings, store, client, max_files=int(batch) or None)
    if st.sidebar.button("Vérifier les nouveautés", width="stretch"):
        list_remote.clear()
        try:
            plan = plan_sync(store, list_remote(settings.cloudinary.cache_key(), client))
            st.sidebar.info(f"{len(plan.to_index)} PDF à indexer · {plan.unchanged} inchangés · {len(plan.to_remove)} supprimés")
        except SourcingError as exc:
            st.sidebar.error(str(exc))


def sidebar_upload(settings: Settings) -> None:
    if not settings.cloudinary_ready:
        return
    client = get_cloudinary(settings.cloudinary.cache_key(), settings)
    with st.sidebar.expander("Ajouter un gros catalogue"):
        sub = st.text_input("Sous-dossier", placeholder="viennoiseries/mon-fournisseur")
        uploaded = st.file_uploader("PDF", type=["pdf"], accept_multiple_files=False)
        st.caption("Optimisation (si PyMuPDF), découpage sous 9 Mo et envoi parallèle automatique. "
                   "Nommez le fichier « FOURNISSEUR_catalogue.pdf » : le fournisseur est lu au début du nom.")
        job: UploadJob | None = st.session_state.get("upload_job")
        if st.button("Optimiser et envoyer", width="stretch"):
            if uploaded is None:
                st.warning("Sélectionnez un PDF.")
            else:
                folder = "/".join(p for p in (settings.cloudinary.prefix, sub.strip("/ ")) if p)
                job = _prepare_job(uploaded, folder)
                if job is not None:
                    _send_job(client, job)
        elif job is not None and job.pending() and st.button("Reprendre les parties restantes", width="stretch"):
            _send_job(client, job)
        for result in (job.failed if job is not None else []):
            st.error(f"{result.part.path.name} : {result.message}")


def _prepare_job(uploaded: Any, folder: str) -> UploadJob | None:
    work = Path(tempfile.mkdtemp(prefix="upload_"))
    source = work / "source.pdf"
    with source.open("wb") as handle:  # copie par blocs : le PDF n'est pas dupliqué en mémoire
        shutil.copyfileobj(uploaded, handle)
    bar = st.progress(0.0, text="Découpage du PDF…")
    try:
        job = prepare_upload(source, work / "parts", stem=Path(uploaded.name).stem, folder=folder,
                             progress=lambda done, total: bar.progress(done / total, text=f"Découpage {done}/{total} pages"))
    except SourcingError as exc:
        st.error(f"Échec : {exc}")
        return None
    finally:
        bar.empty()
        source.unlink(missing_ok=True)
    st.session_state["upload_job"] = job
    st.info(f"{len(job.parts)} partie(s) prête(s) (la plus lourde : {max(p.size_bytes for p in job.parts) / 1e6:.1f} Mo).")
    return job


def _send_job(client: CloudinaryClient, job: UploadJob) -> None:
    bar = st.progress(0.0, text="Envoi vers Cloudinary…")
    try:
        run_upload(client, job, on_progress=lambda done, total, res: bar.progress(
            done / max(total, 1), text=f"{done}/{total} · {res.part.path.name[-30:]} ({res.status})"))
    except SourcingError as exc:
        st.error(str(exc))
    finally:
        bar.empty()
    list_remote.clear()
    if job.complete:
        st.success(f"Envoi terminé : {len(job.parts)} partie(s). Cliquez sur Synchroniser.")
        shutil.rmtree(job.parts[0].path.parent, ignore_errors=True)
        st.session_state.pop("upload_job", None)
    else:
        st.warning(f"Envoi incomplet : {job_summary(job)}")


# ---------------------------------------------------------------------------
# Besoin et catalogues
# ---------------------------------------------------------------------------


def describe_plan(plan: QueryPlan) -> str:
    parts = [f"produit « {plan.product_text} »" if plan.product_text else "produit non précisé"]
    if plan.weight_g:
        parts.append(f"grammage {plan.weight_g:g} g")
    if plan.references:
        parts.append("référence " + ", ".join(plan.references))
    if plan.modifiers:
        parts.append("critères : " + ", ".join(plan.modifiers))
    if plan.countries:
        parts.append("pays : " + ", ".join(plan.countries))
    if plan.families:
        parts.append("famille " + ", ".join(f.label_fr for f in plan.families))
    return "Besoin compris : " + " · ".join(parts)


def tab_need(store: IndexStore) -> None:
    product = st.text_input("Produit recherché", placeholder="Mini Danish assortiment", key="product")
    weight = st.text_input("Grammage", placeholder="40 g", key="weight")
    criteria = st.text_area("Critères", placeholder="Surgelé, assortiment, private label, Italie...", key="criteria")
    with st.expander("Filtres et tolérance (catalogues)"):
        suppliers = st.multiselect("Fournisseurs", store.suppliers())
        low, high = st.columns(2)
        min_w = low.number_input("Grammage min (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0)
        max_w = high.number_input("Grammage max (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0)
        tolerance = st.slider("Tolérance de grammage (0,15 = ±15 % autour de la cible)", 0.05, 0.40, 0.15, 0.05)
        only_structured = st.checkbox("Uniquement les fiches produit structurées")
    limit = st.slider("Résultats", 5, 100, 30, 5)
    query = " ".join(filter(None, [product.strip(), weight.strip(), criteria.strip()]))
    plan = build_query_plan(query)
    if not plan.is_empty:
        st.caption(describe_plan(plan))
    if st.button("Rechercher dans les catalogues", type="primary", width="stretch"):
        if not product.strip():
            st.warning("Indiquez le produit.")
        elif store.stats().indexed == 0:
            st.error("Aucune page indexée : synchronisez d'abord les catalogues (barre latérale).")
        else:
            st.session_state["need"] = {"query": query}
            st.session_state["hits"] = cached_internal_search(
                store.version(), query, tuple(suppliers), min_w or None, max_w or None, only_structured, tolerance, limit, store)
            st.session_state.pop("web_report", None)
            st.success("Recherche terminée : voir l'onglet Catalogues.")


def tab_catalogues(settings: Settings, store: IndexStore) -> None:
    hits: list[InternalHit] | None = st.session_state.get("hits")
    if hits is None:
        st.info("Lancez une recherche depuis l'onglet Besoin.")
        return
    if not hits:
        st.warning("Aucun passage pertinent dans les catalogues indexés. Essayez un terme plus général ou passez à l'onglet Industriels.")
        return
    st.caption(f"{len(hits)} résultat(s), classés par score explicable (points / maximum applicable).")
    client = get_cloudinary(settings.cloudinary.cache_key(), settings) if settings.cloudinary_ready else None
    today = date.today().isoformat()
    for hit in hits:
        render_internal_hit(hit, today, client, store)


def render_internal_hit(hit: InternalHit, today: str, client: CloudinaryClient | None, store: IndexStore) -> None:
    with st.container(border=True):
        top, right = st.columns([5, 1])
        top.markdown(f"**{hit.title}**  \n{hit.supplier} · {hit.catalog} · {hit.page_label}")
        right.metric("Score", hit.score_label)
        facts = []
        if hit.refs:
            facts.append("Réf. " + ", ".join(hit.refs))
        if hit.weights_g:
            facts.append(", ".join(f"{w:g} g" for w in hit.weights_g))
        facts.extend(hit.packaging[:2])
        if facts:
            st.write(" · ".join(facts))
        st.caption(hit.excerpt)
        if hit.duplicates:
            st.caption(f"{hit.duplicates} passage(s) identique(s) regroupé(s).")
        with st.expander("Pourquoi ce score ?"):
            rows = [{"Composante": k, "Points": v} for k, v in hit.components.items()]
            rows += [{"Composante": f"pénalité : {k}", "Points": -v} for k, v in hit.penalties.items()]
            st.table(rows)
            for note in hit.notes:
                st.caption("• " + note)
        render_preview(hit, client, store)
        decision_buttons("int", hit.key, row_from_internal(hit, today))


def _show_preview(flag: str) -> None:
    st.session_state[flag] = True


def render_preview(hit: InternalHit, client: CloudinaryClient | None, store: IndexStore) -> None:
    """Aperçu image de la page (comme l'application d'origine), à la demande pour limiter les téléchargements."""
    if client is None or not pdf_render.fitz_available():
        st.caption("Aperçu de la page indisponible (Cloudinary ou PyMuPDF non configuré).")
        return
    flag = f"preview-{hit.key}"
    st.button("Afficher l'aperçu de la page", key=f"btn-{flag}", on_click=_show_preview, args=(flag,))
    if st.session_state.get(flag):
        try:
            st.image(page_image(hit.key, hit.public_id, hit.local_page, client, store), width="stretch")
        except SourcingError as exc:
            st.warning(f"Aperçu indisponible : {exc}")


# ---------------------------------------------------------------------------
# Industriels (Internet)
# ---------------------------------------------------------------------------


def run_web_search(settings: Settings, store: IndexStore, plan: QueryPlan, options: WebSearchOptions, use_base: bool) -> WebSearchReport | None:
    cache = web_cache()
    cache_key = (plan.cache_key(), options.max_queries, options.include_known, options.europe_only, use_base, store.version())
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    manufacturers = load_verified_manufacturers(settings.manufacturers_path) if use_base else []
    engine = WebSourcing(TavilyClient(settings.tavily_api_key), manufacturers=manufacturers,
                         directory=known_directory(settings, store), options=options)
    bar = st.progress(0.0, text="Recherche Internet…")
    try:
        report = engine.run(plan, progress=lambda label, fraction: bar.progress(fraction, text=label))
    except SourcingError as exc:
        st.error(f"Recherche impossible : {exc}")
        return None
    finally:
        bar.empty()
    cache.set(cache_key, report)
    return report


def tab_web(settings: Settings, store: IndexStore) -> None:
    need = st.session_state.get("need")
    if not need:
        st.info("Lancez d'abord une recherche depuis l'onglet Besoin : la recherche Internet s'adapte à ce besoin.")
        return
    plan = build_query_plan(need["query"])
    st.caption("Seules des sources officielles (site du fabricant) peuvent confirmer un fabricant. Rien n'est inventé : "
               f"« {NOT_FOUND_PUBLIC} » est affiché quand aucune preuve n'est trouvée.")
    if not settings.tavily_ready:
        st.info("Ajoutez TAVILY_API_KEY (clé « tvly-… ») dans les secrets pour activer la recherche Internet.")
        return
    max_queries = st.slider("Budget de requêtes Tavily pour cette recherche", 6, 30, 18, 2)
    include_known = st.checkbox("Inclure les fournisseurs déjà référencés (ils sont sinon écartés avant toute requête)")
    europe_only = st.checkbox("Europe uniquement", value=True)
    use_base = st.checkbox("Utiliser ma base de fabricants vérifiés (optionnel, vide par défaut)")
    if st.button("Rechercher de nouveaux industriels", type="primary", width="stretch"):
        options = WebSearchOptions(max_queries=max_queries, include_known=include_known, europe_only=europe_only)
        st.session_state["web_report"] = run_web_search(settings, store, plan, options, use_base)
    report: WebSearchReport | None = st.session_state.get("web_report")
    if report is None:
        return
    st.caption(f"{len(report.candidates)} candidat(s) · {report.queries_used} requête(s) Tavily · "
               f"{report.excluded_results} résultat(s) écartés (marketplaces, recettes, réseaux sociaux, hors Europe...) · "
               f"vérifié le {report.checked_on}")
    for warning in report.warnings[:5]:
        st.warning(warning)
    render_web_transparency(report)
    shown = filter_candidates(report.candidates)
    for candidate in shown:
        render_candidate(candidate)
    if shown:
        st.download_button("Exporter CSV", to_csv_bytes([("", row_from_candidate(c)) for c in shown]),
                           "industriels.csv", "text/csv", width="stretch")


def render_web_transparency(report: WebSearchReport) -> None:
    with st.expander(f"Comment la recherche a été conduite ({len(report.queries)} requêtes de découverte)"):
        for query in report.queries:
            st.caption("• " + query)
        if report.learned_terms:
            st.write("Variantes du produit apprises dans les premiers résultats : " + ", ".join(report.learned_terms))
    if report.known_skipped:
        with st.expander(f"{len(report.known_skipped)} fournisseur(s) déjà référencé(s) écarté(s) sans dépenser de requête"):
            for company, domain, known in report.known_skipped:
                st.caption(f"• {company} ({domain}) → déjà dans votre liste : {known}")


def filter_candidates(candidates: list[SupplierCandidate]) -> list[SupplierCandidate]:
    with st.expander("Filtres"):
        countries = st.multiselect("Pays", sorted({c.country or "à confirmer" for c in candidates}))
        categories = st.multiselect("Catégorie", sorted({cat for c in candidates for cat in c.categories}))
        statuses = st.multiselect("Statut industriel", STATUSES)
        known = st.radio("Fournisseur", ["Tous", "Déjà connu", "Nouveau"], horizontal=True)
        low, high = st.columns(2)
        min_w = low.number_input("Grammage min trouvé (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0, key="web_min_w")
        max_w = high.number_input("Grammage max trouvé (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0, key="web_max_w")
    result = []
    for c in candidates:
        if countries and (c.country or "à confirmer") not in countries:
            continue
        if categories and not set(categories) & set(c.categories):
            continue
        if statuses and c.status not in statuses:
            continue
        if (known == "Déjà connu" and not c.known_supplier) or (known == "Nouveau" and c.known_supplier):
            continue
        if (min_w or max_w) and not any((min_w or 0) <= w <= (max_w or float("inf")) for w in c.weights_g):
            continue
        result.append(c)
    return result


def link_line(label: str, url: str | None) -> str:
    return f"[{label}]({url})" if url else f"{label} : {NOT_FOUND_PUBLIC}"


def render_candidate(candidate: SupplierCandidate) -> None:
    with st.container(border=True):
        top, right = st.columns([5, 1])
        icon = STATUS_ICONS.get(candidate.status, "⚪")
        badge = " · base vérifiée" if candidate.from_verified_base else ""
        known = f"déjà référencé ({candidate.known_as})" if candidate.known_supplier else "nouveau"
        top.markdown(f"**{candidate.company}** ({candidate.domain}) — {icon} {candidate.status}{badge}  \n"
                     f"{candidate.country or 'Pays à confirmer'} · {known} · vérifié le {candidate.checked_on}")
        right.metric("Score", candidate.score_label)
        st.markdown(" · ".join([link_line("Page produit", candidate.product_page_url),
                                link_line("Fiche / catalogue", candidate.datasheet_url),
                                link_line("Contact", candidate.contact_url)]))
        if candidate.similar_product:
            st.caption(f"Produit proche : {candidate.similar_product}")
        st.caption(f"Preuve industrielle : {candidate.industrial_level.value} · produit : {candidate.product_level.value} · "
                   f"private label : {candidate.private_label.value} · foodservice : {candidate.foodservice.value}")
        with st.expander("Preuves, score et points à confirmer"):
            for evidence in [*candidate.industrial_evidence, *candidate.product_evidence]:
                st.markdown(f"- **{evidence.kind}** ({evidence.origin}, {evidence.checked_on}) : [{evidence.url}]({evidence.url})")
                if evidence.snippet:
                    st.caption(evidence.snippet)
            if not (candidate.industrial_evidence or candidate.product_evidence):
                st.write(NOT_FOUND_PUBLIC)
            rows = [{"Composante": k, "Points": v} for k, v in candidate.components.items()]
            rows += [{"Composante": f"pénalité : {k}", "Points": -v} for k, v in candidate.penalties.items()]
            st.table(rows)
            st.write("**À confirmer avant toute décision :** " + ", ".join(candidate.to_confirm))
            for note in candidate.notes:
                st.caption("• " + note)
        decision_buttons("web", candidate.key, row_from_candidate(candidate))


# ---------------------------------------------------------------------------
# Shortlist et bibliothèque
# ---------------------------------------------------------------------------


def tab_shortlist() -> None:
    decisions: dict[str, tuple[str, dict[str, str]]] = st.session_state.get("decisions", {})
    if not decisions:
        st.info("Marquez des résultats « Pertinent » ou « À étudier » dans les onglets Catalogues et Industriels.")
        return
    keep = st.multiselect("Décisions à exporter", list(DECISIONS), default=["Pertinent", "À étudier"])
    entries = [(label, row) for label, row in decisions.values() if label in keep]
    st.write(f"{len(entries)} ligne(s) sélectionnée(s) sur {len(decisions)}.")
    st.dataframe(
        [{"Décision": d, "Origine": r["origine"], "Fournisseur": r["fournisseur"], "Produit": r["produit"],
          "Statut": r["statut"], "Score": r["score (points)"], "Date": r["date de vérification"]} for d, r in entries],
        width="stretch", hide_index=True,
    )
    st.download_button("Télécharger la shortlist (CSV, Excel FR)", to_csv_bytes(entries),
                       file_name=f"shortlist_sourcing_{date.today().isoformat()}.csv", mime="text/csv",
                       disabled=not entries, width="stretch")
    if st.button("Vider la shortlist"):
        st.session_state["decisions"] = {}
        st.rerun()


def tab_library(store: IndexStore) -> None:
    records = store.list_files()
    if not records:
        st.info("Aucun catalogue synchronisé.")
        return
    labels = {"indexed": "OK", "needs_ocr": "OCR requis", "error": "Erreur"}
    st.dataframe(
        [{"Fournisseur": r.supplier, "Catalogue": r.public_id, "Taille Mo": round((r.size_bytes or 0) / 1024 / 1024, 2),
          "Pages": r.page_count, "Passages": r.passages,
          "Statut": labels.get(r.status, r.status) + (f" : {r.status_detail}" if r.status != "indexed" and r.status_detail else "")}
         for r in records],
        width="stretch", hide_index=True,
    )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="AI Sourcing Délifrance", page_icon="🔎", layout="wide")
    st.title("🔎 AI Sourcing Délifrance")
    st.caption("Bibliothèque Cloudinary · Recherche catalogues · Sourcing industriel")
    settings = load_settings()
    store = get_store(str(settings.db_path))
    status_box = st.sidebar.container()
    sidebar_sync(settings, store)
    sidebar_upload(settings)
    sidebar_status(status_box, settings, store)
    need_tab, result_tab, web_tab, shortlist_tab, library_tab = st.tabs(
        ["🎯 Besoin", "📁 Catalogues", "🌍 Industriels", "✅ Shortlist", "📚 Bibliothèque"])
    with need_tab:
        tab_need(store)
    with result_tab:
        tab_catalogues(settings, store)
    with web_tab:
        tab_web(settings, store)
    with shortlist_tab:
        tab_shortlist()
    with library_tab:
        tab_library(store)
    st.divider()
    st.caption("Outil d'aide au sourcing pour l'acheteur tiers Délifrance. Vérifier chaque information avant contact.")


if __name__ == "__main__":
    main()
