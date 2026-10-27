"""AI Sourcing Délifrance : interface Streamlit.

Ce fichier ne contient que l'interface. La logique est dans les modules ``sourcing_*.py`` (index, recherche, scoring, Internet, images)
et ``cloudinary_pdf_tools.py`` (stockage des catalogues PDF). Parcours en deux étapes : base interne, puis industriels.
"""

from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

from cloudinary_pdf_tools import CloudinaryClient, RemotePdf, UploadJob, job_summary, prepare_upload, run_upload
import sourcing_pdf_extract as pdf_extract
from sourcing_backup import CloudinaryState
from sourcing_cache import BoundedTTLCache
from sourcing_errors import SourcingError
from sourcing_images import ImageDescriptor, require_descriptor
from sourcing_indexer import plan_sync, sync_index
from sourcing_internal_search import InternalHit, ScoringConfig, SearchFilters, search_internal
from sourcing_manufacturers import load_verified_manufacturers
from sourcing_models import NOT_FOUND_PUBLIC, STATUS_ICONS, STATUSES, SupplierCandidate
from sourcing_settings import Settings, redact
from sourcing_shortlist import DECISIONS, merge_decisions, row_from_candidate, row_from_internal, to_csv_bytes
from sourcing_store import IndexStore
from sourcing_suppliers import KnownSupplier, SupplierDirectory
from sourcing_textutils import COUNTRY_LABELS, QueryPlan, build_query_plan
from sourcing_vision_ai import VisualBrief, describe_image
from sourcing_web_search import (LIST_EXCLUDE, LIST_FLAG_ONLY, LIST_PRIORITY, TavilyClient, WebSearchOptions, WebSearchReport,
                                 WebSourcing)

SAVE_DELAY_SECONDS = 8.0


# ---------------------------------------------------------------------------
# Ressources partagées (une seule fois par processus, jamais de secret affiché)
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    return {k: _plain(v) for k, v in value.items()} if hasattr(value, "items") else value


def load_settings() -> Settings:
    """Secrets Streamlit (``.streamlit/secrets.toml``) puis variables d'environnement."""
    try:
        secrets = {key: _plain(st.secrets[key]) for key in st.secrets}
    except Exception:  # noqa: BLE001 - pas de fichier secrets = configuration vide, pas une erreur
        secrets = {}
    return Settings.from_mapping(secrets)


@st.cache_resource(show_spinner=False)
def get_store(db_path: str) -> IndexStore:
    return IndexStore(db_path)


@st.cache_resource(show_spinner=False)
def get_cloudinary(key: str, _settings: Settings) -> CloudinaryClient:
    """``key`` (empreinte non réversible des identifiants) invalide le cache quand les secrets changent."""
    return CloudinaryClient(_settings.cloudinary)


@st.cache_resource(show_spinner=False)
def get_state(key: str, _client: CloudinaryClient, prefix: str) -> CloudinaryState:
    return CloudinaryState(_client, prefix)


@st.cache_resource(show_spinner=False)
def get_flags() -> dict[str, Any]:
    """Indicateurs partagés par tous les visiteurs du processus (dernière version d'index sauvegardée…)."""
    return {"saved_version": None, "restore_message": None}


@st.cache_resource(show_spinner=False)
def get_search_cache() -> BoundedTTLCache[list[InternalHit]]:
    return BoundedTTLCache(max_entries=32, ttl_seconds=900)


@st.cache_resource(show_spinner=False)
def get_web_cache() -> BoundedTTLCache[WebSearchReport]:
    return BoundedTTLCache(max_entries=6, ttl_seconds=3600)


@st.cache_data(ttl=600, max_entries=2, show_spinner=False)
def list_remote(key: str, _client: CloudinaryClient) -> list[RemotePdf]:
    return _client.list_pdfs()


@st.cache_data(max_entries=4, show_spinner=False)
def photo_descriptor(digest: str, _data: bytes) -> ImageDescriptor:
    return require_descriptor(_data)


@st.cache_data(show_spinner=False, ttl=1800, max_entries=4)
def page_image(key: str, public_id: str, local_page: int, _client: CloudinaryClient, _store: IndexStore) -> bytes:
    """Aperçu PNG d'une page : le PDF (une partie < 10 Mo) est téléchargé à la demande puis supprimé."""
    record = _store.get_file(public_id)
    if record is None:
        raise SourcingError("Ce PDF n'est plus dans l'index.")
    remote = RemotePdf(record.public_id, record.version or "", record.size_bytes or 0, record.etag or "",
                       record.resource_type or "raw", record.secure_url or "")
    with tempfile.TemporaryDirectory(prefix="preview_") as tmp:
        target = Path(tmp) / "page.pdf"
        _client.download_to_file(remote, target)
        return pdf_extract.render_page_png(target, local_page)


def known_directory(settings: Settings, store: IndexStore) -> SupplierDirectory:
    """Fournisseurs de la liste de l'acheteur + fournisseurs des catalogues déjà indexés."""
    return SupplierDirectory.from_file(settings.known_suppliers_path).with_names(store.suppliers())


def show_error(exc: Exception, settings: Settings) -> None:
    st.error(redact(exc, settings.secret_values()))


# ---------------------------------------------------------------------------
# Décisions et shortlist (partagée via Cloudinary)
# ---------------------------------------------------------------------------


def set_decision(key: str, label: str, row: dict[str, str]) -> None:
    decisions: dict[str, tuple[str, dict[str, str]]] = st.session_state.setdefault("decisions", {})
    removed: set[str] = st.session_state.setdefault("removed", set())
    if decisions.get(key, ("", {}))[0] == label:
        decisions.pop(key, None)  # un second clic annule la décision
        removed.add(key)
    else:
        row = {**row, "décidé par": st.session_state.get("user_name", "")}
        decisions[key] = (label, row)
        removed.discard(key)
    st.session_state["dirty"] = True


def decision_buttons(prefix: str, key: str, row: dict[str, str]) -> None:
    current = st.session_state.get("decisions", {}).get(key, ("", {}))[0]
    for column, label in zip(st.columns(len(DECISIONS)), DECISIONS):
        column.button(f"{label} ✓" if current == label else label, key=f"{prefix}-{key}-{label}", width="stretch",
                      type="primary" if current == label else "secondary", on_click=set_decision, args=(key, label, row))


def load_shared_shortlist(settings: Settings) -> None:
    """Charge une fois par session la shortlist partagée enregistrée sur Cloudinary."""
    if st.session_state.get("shortlist_loaded") or not settings.cloudinary_ready:
        return
    st.session_state["shortlist_loaded"] = True
    try:
        client = get_cloudinary(settings.cloudinary.cache_key(), settings)
        remote = get_state(settings.cloudinary.cache_key(), client, settings.cloudinary.prefix).load_shortlist()
        st.session_state["decisions"] = merge_decisions(remote, st.session_state.get("decisions", {}))
    except SourcingError as exc:
        st.session_state["shortlist_notice"] = f"Shortlist partagée indisponible : {redact(exc, settings.secret_values())}"


def save_shared_shortlist(settings: Settings, *, force: bool = False) -> str | None:
    """Fusionne avec la version partagée puis l'enregistre. Au plus une sauvegarde toutes les 8 s sauf ``force``."""
    if not settings.cloudinary_ready or not st.session_state.get("dirty"):
        return None
    if not force and time.time() - st.session_state.get("last_save", 0.0) < SAVE_DELAY_SECONDS:
        return None
    try:
        client = get_cloudinary(settings.cloudinary.cache_key(), settings)
        state = get_state(settings.cloudinary.cache_key(), client, settings.cloudinary.prefix)
        merged = merge_decisions(state.load_shortlist(), st.session_state.get("decisions", {}), st.session_state.get("removed", set()))
        state.save_shortlist(merged)
        st.session_state["decisions"] = merged
        st.session_state["removed"] = set()
        st.session_state["dirty"] = False
        st.session_state["last_save"] = time.time()
        return None
    except SourcingError as exc:
        return f"Sauvegarde de la shortlist impossible : {redact(exc, settings.secret_values())}"


# ---------------------------------------------------------------------------
# Barre latérale : configuration, synchronisation, envoi de PDF
# ---------------------------------------------------------------------------


def restore_once(settings: Settings, store: IndexStore) -> None:
    """Au premier passage après un redémarrage : restaure l'index sauvegardé sur Cloudinary (évite tout ré-indexer)."""
    flags = get_flags()
    if flags["restore_message"] is not None or not settings.cloudinary_ready:
        return
    flags["restore_message"] = ""
    if not store.is_empty():
        return
    try:
        client = get_cloudinary(settings.cloudinary.cache_key(), settings)
        with st.spinner("Restauration de l'index depuis Cloudinary…"):
            restored = get_state(settings.cloudinary.cache_key(), client, settings.cloudinary.prefix).restore_index(store)
        flags["restore_message"] = "Index restauré depuis Cloudinary." if restored else ""
        flags["saved_version"] = store.version() if restored else None
    except SourcingError as exc:
        flags["restore_message"] = f"Restauration impossible : {redact(exc, settings.secret_values())}"


def backup_index(settings: Settings, store: IndexStore) -> None:
    flags = get_flags()
    if flags["saved_version"] == store.version() or not settings.cloudinary_ready:
        return
    try:
        client = get_cloudinary(settings.cloudinary.cache_key(), settings)
        with st.spinner("Sauvegarde de l'index sur Cloudinary…"):
            message = get_state(settings.cloudinary.cache_key(), client, settings.cloudinary.prefix).save_index(store)
        flags["saved_version"] = store.version()
        st.sidebar.caption(message)
    except SourcingError as exc:
        st.sidebar.warning(f"Sauvegarde de l'index impossible : {redact(exc, settings.secret_values())}")


def sidebar_status(box: Any, settings: Settings, store: IndexStore) -> None:
    """Remplit le conteneur de statut (créé en haut de la barre latérale, rempli après la synchronisation)."""
    box.header("⚙️ Configuration")
    if settings.cloudinary_ready:
        box.write(f"✅ Cloudinary · dossier « {settings.cloudinary.prefix or 'racine'} »")
    else:
        box.error("Cloudinary non configuré (Secrets Streamlit) : " + ", ".join(settings.cloudinary.missing()) + ".")
    box.write("✅ Tavily configuré" if settings.tavily_ready else "⚪ Tavily non configuré (TAVILY_API_KEY, « tvly-… »)")
    provider = {"gemini": "Gemini (gratuit)", "openai": "OpenAI (payant)"}.get(settings.ai_provider or "")
    box.write(f"✅ IA photo : {provider}" if provider else "⚪ IA photo non configurée (optionnelle, la recherche image fonctionne sans)")
    stats = store.stats()
    left, right = box.columns(2)
    left.metric("PDF indexés", f"{stats.indexed}/{stats.files}")
    right.metric("Passages", stats.passages)
    box.caption(f"{stats.images} photo(s) indexée(s)" + (f" · {stats.needs_ocr} PDF scanné(s)" if stats.needs_ocr else "")
                + (f" · {stats.errors} en erreur" if stats.errors else ""))
    if not store.fts_available:
        box.warning("SQLite sans FTS5 : recherche en mode de repli (plus lente).")
    message = get_flags().get("restore_message")
    if message:
        box.caption(message)
    if not pdf_extract.fitz_available():
        box.warning("PyMuPDF absent : indexation et aperçus impossibles (pip install pymupdf).")


def run_sync(settings: Settings, store: IndexStore, client: CloudinaryClient, *, max_files: int | None, retry_errors: bool = False) -> None:
    bar = st.sidebar.progress(0.0, text="Liste des PDF…")
    try:
        list_remote.clear()
        remotes = list_remote(settings.cloudinary.cache_key(), client)
        report = sync_index(
            store, client, remotes, max_files=max_files, directory=known_directory(settings, store), retry_errors=retry_errors,
            ocr=st.session_state.get("ocr_enabled", False), ocr_languages=settings.ocr_languages,
            progress=lambda i, n, name: bar.progress(i / max(n, 1), text=f"{i}/{n} · {name.rsplit('/', 1)[-1][:40]}"))
        st.sidebar.success(report.summary())
        st.session_state["sync_remaining"] = report.remaining
        for public_id, message in report.errors:
            st.sidebar.error(f"{public_id.rsplit('/', 1)[-1]} : {redact(message, settings.secret_values())}")
        if report.changed:
            backup_index(settings, store)
    except SourcingError as exc:
        st.sidebar.error(f"Erreur Cloudinary : {redact(exc, settings.secret_values())}")
    finally:
        bar.empty()


def sidebar_sync(settings: Settings, store: IndexStore) -> None:
    if not settings.cloudinary_ready:
        return
    client = get_cloudinary(settings.cloudinary.cache_key(), settings)
    st.sidebar.divider()
    st.sidebar.subheader("🔄 Catalogues")
    auto = st.sidebar.checkbox("Synchroniser au démarrage", value=True, help="Seuls les PDF nouveaux ou modifiés sont retraités.")
    batch = st.sidebar.number_input("PDF par mise à jour (0 = tous)", min_value=0, max_value=300, value=20, step=5)
    st.sidebar.checkbox("OCR des PDF scannés (si Tesseract installé)", key="ocr_enabled", help="Voir le README : packages.txt.")
    if auto and not st.session_state.get("auto_synced"):
        st.session_state["auto_synced"] = True
        run_sync(settings, store, client, max_files=int(batch) or None)
    if st.sidebar.button("Synchroniser", type="primary", width="stretch"):
        run_sync(settings, store, client, max_files=int(batch) or None)
    if st.session_state.get("sync_remaining"):
        st.sidebar.info(f"{st.session_state['sync_remaining']} PDF restant(s) : cliquez de nouveau sur Synchroniser.")
    left, right = st.sidebar.columns(2)
    if left.button("Nouveautés", width="stretch"):
        list_remote.clear()
        try:
            plan = plan_sync(store, list_remote(settings.cloudinary.cache_key(), client))
            st.sidebar.info(f"{len(plan.to_index)} PDF à indexer · {plan.unchanged} inchangés · {len(plan.to_remove)} supprimés")
        except SourcingError as exc:
            show_error(exc, settings)
    if right.button("Retenter erreurs", width="stretch"):
        run_sync(settings, store, client, max_files=int(batch) or None, retry_errors=True)
    if st.sidebar.button("Tester la connexion Cloudinary", width="stretch"):
        try:
            st.sidebar.success(client.ping())
        except SourcingError as exc:
            show_error(exc, settings)


def sidebar_upload(settings: Settings) -> None:
    if not settings.cloudinary_ready:
        return
    client = get_cloudinary(settings.cloudinary.cache_key(), settings)
    with st.sidebar.expander("Ajouter un gros catalogue"):
        sub = st.text_input("Sous-dossier", placeholder="viennoiseries/mon-fournisseur")
        uploaded = st.file_uploader("PDF", type=["pdf"], accept_multiple_files=False)
        st.caption("Découpage automatique sous 9 Mo (limite gratuite Cloudinary 10 Mo), envoi parallèle et reprise. "
                   "Nommez le fichier « FOURNISSEUR_catalogue.pdf » : le fournisseur est lu au début du nom.")
        job: UploadJob | None = st.session_state.get("upload_job")
        if st.button("Optimiser et envoyer", width="stretch"):
            if uploaded is None:
                st.warning("Sélectionnez un PDF.")
            else:
                folder = "/".join(p for p in (settings.cloudinary.prefix, sub.strip("/ ")) if p)
                job = _prepare_job(uploaded, folder, settings)
                if job is not None:
                    _send_job(client, job, settings)
        elif job is not None and job.pending() and st.button("Reprendre les parties restantes", width="stretch"):
            _send_job(client, job, settings)
        for part in (job.failed if job is not None else []):
            st.error(f"{part.path.name} : {redact(part.message, settings.secret_values())}")


def _prepare_job(uploaded: Any, folder: str, settings: Settings) -> UploadJob | None:
    work = Path(tempfile.mkdtemp(prefix="upload_"))
    source = work / "source.pdf"
    with source.open("wb") as handle:  # copie par blocs : le PDF n'est pas dupliqué en mémoire
        shutil.copyfileobj(uploaded, handle)
    bar = st.progress(0.0, text="Découpage du PDF…")
    try:
        job = prepare_upload(source, work / "parts", stem=Path(uploaded.name).stem, folder=folder,
                             progress=lambda done, total: bar.progress(done / max(total, 1), text=f"Découpage {done}/{total} pages"))
    except SourcingError as exc:
        show_error(exc, settings)
        return None
    finally:
        bar.empty()
        source.unlink(missing_ok=True)
    st.session_state["upload_job"] = job
    st.info(f"{len(job.parts)} partie(s) prête(s) (la plus lourde : {max(p.size_bytes for p in job.parts) / 1e6:.1f} Mo).")
    return job


def _send_job(client: CloudinaryClient, job: UploadJob, settings: Settings) -> None:
    bar = st.progress(0.0, text="Envoi vers Cloudinary…")
    try:
        run_upload(client, job, on_progress=lambda done, total, part: bar.progress(
            done / max(total, 1), text=f"{done}/{total} · {part.path.name[-30:]} ({part.status})"))
    except SourcingError as exc:
        show_error(exc, settings)
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
# Onglet Besoin
# ---------------------------------------------------------------------------


def describe_plan(plan: QueryPlan) -> str:
    parts = [f"produit « {plan.product_text} »" if plan.product_text else "produit non précisé"]
    if plan.weight_g:
        parts.append(f"grammage {plan.weight_g:g} g")
    if plan.references:
        parts.append("référence " + ", ".join(plan.references))
    if plan.modifiers:
        parts.append("critères : " + ", ".join(m.label_fr for m in plan.modifiers))
    if plan.countries:
        parts.append("pays : " + ", ".join(COUNTRY_LABELS.get(c, c) for c in plan.countries))
    if plan.families:
        parts.append("famille " + ", ".join(f.label_fr for f in plan.families))
    return "Besoin compris : " + " · ".join(parts)


def analyse_photo(settings: Settings, data: bytes) -> None:
    """Rappel du bouton « Analyser avec l'IA » : remplit la description IA avant l'affichage des champs."""
    st.session_state.pop("brief_error", None)
    try:
        brief = describe_image(settings, data)
    except SourcingError as exc:
        st.session_state["brief_error"] = redact(exc, settings.secret_values())
        return
    st.session_state["brief"] = brief
    st.session_state["brief_text"] = ((brief.description + "\n") if brief.description else "") + brief.as_query_text()


def photo_section(settings: Settings) -> tuple[ImageDescriptor | None, str, VisualBrief | None]:
    """Photo cible facultative : descripteur visuel gratuit + description IA facultative."""
    photo = st.file_uploader("📷 Photo du produit cible (facultatif)", type=["jpg", "jpeg", "png", "webp"], key="photo",
                             help="La recherche image compare couleur, forme et texture avec les photos des catalogues et des sites fabricants.")
    if photo is None:
        for key in ("brief", "brief_text", "brief_error"):
            st.session_state.pop(key, None)
        return None, "", None
    data = photo.getvalue()
    digest = hashlib.sha1(data).hexdigest()[:12]
    try:
        descriptor = photo_descriptor(digest, data)
    except SourcingError as exc:
        show_error(exc, settings)
        return None, "", None
    left, right = st.columns([1, 3])
    left.image(data, width=200)
    with right:
        if settings.ai_provider:
            st.button("🤖 Analyser la photo avec l'IA (facultatif)", on_click=analyse_photo, args=(settings, data))
            st.caption("La photo est envoyée au fournisseur d'IA choisi (Gemini gratuit, sinon OpenAI payant).")
        else:
            st.caption("Sans clé d'IA, la recherche image reste active (couleur, forme, texture). Ajoutez GEMINI_API_KEY (gratuit) "
                       "pour obtenir en plus une description automatique du produit.")
        if st.session_state.get("brief_error"):
            st.warning(st.session_state["brief_error"])
        if "brief_text" in st.session_state:
            st.text_area("Description issue de la photo (modifiable, ajoutée à votre besoin)", key="brief_text", height=120)
    brief: VisualBrief | None = st.session_state.get("brief")
    return descriptor, st.session_state.get("brief_text", ""), brief


def tab_need(settings: Settings, store: IndexStore) -> None:
    st.markdown("**1. Décrivez le produit cible** : plus la description est précise (recette, visuel, grammage), meilleurs sont les résultats.")
    product = st.text_input("Produit recherché", placeholder="Mini Danish assortiment", key="product")
    description = st.text_area("Description détaillée", key="description", height=130,
                               placeholder="Pâte feuilletée pur beurre, fourrage abricot / pomme / crème pâtissière, décor nappage brillant, "
                                           "carton de 120 pièces, volume annuel 200 t, IFS/BRC, private label...")
    left, right = st.columns(2)
    weight = left.text_input("Grammage", placeholder="40 g", key="weight")
    criteria = right.text_input("Critères", placeholder="Surgelé, prêt à cuire, Italie, private label...", key="criteria")
    descriptor, brief_text, brief = photo_section(settings)
    estimated = brief.estimated_weight_g if brief else None
    use_estimate = False
    if estimated and not weight.strip():
        use_estimate = st.checkbox(f"Utiliser le grammage estimé par l'IA ({estimated:g} g) comme cible", value=False)
    with st.expander("Filtres et tolérance (catalogues)"):
        suppliers = st.multiselect("Fournisseurs", store.suppliers())
        low, high = st.columns(2)
        min_w = low.number_input("Grammage min (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0)
        max_w = high.number_input("Grammage max (g, 0 = aucun)", min_value=0.0, value=0.0, step=5.0)
        tolerance = st.slider("Tolérance de grammage (0,15 = ±15 % autour de la cible)", 0.05, 0.40, 0.15, 0.05)
        only_structured = st.checkbox("Uniquement les fiches produit structurées")
    limit = st.slider("Résultats", 5, 100, 30, 5)
    query = " ".join(filter(None, [product.strip(), description.strip(), weight.strip(), criteria.strip(), brief_text.strip(),
                                   f"{estimated:g} g" if use_estimate and estimated else ""]))
    plan = build_query_plan(query)
    if not plan.is_empty:
        st.caption(describe_plan(plan))
    if descriptor is not None:
        st.caption("📷 Recherche par image activée : le score combine le texte et la similarité visuelle.")
    if st.button("🔎 Rechercher dans les catalogues", type="primary", width="stretch"):
        if plan.is_empty and descriptor is None:
            st.warning("Indiquez le produit ou ajoutez une photo.")
        elif store.stats().indexed == 0:
            st.error("Aucune page indexée : synchronisez d'abord les catalogues (barre latérale).")
        else:
            digest = hashlib.sha1(descriptor.vector.tobytes()).hexdigest()[:12] if descriptor is not None else ""
            key = (store.version(), plan.cache_key(), tuple(suppliers), min_w, max_w, only_structured, tolerance, limit, digest)
            cache = get_search_cache()
            hits = cache.get(key)
            if hits is None:
                hits = search_internal(store, plan, SearchFilters(tuple(suppliers), min_w or None, max_w or None, only_structured),
                                       ScoringConfig(weight_tolerance=tolerance), top_k=limit, image=descriptor)
                cache.set(key, hits)
            st.session_state["need"] = {"query": query, "descriptor": descriptor}
            st.session_state["hits"] = hits
            st.session_state.pop("web_report", None)
            st.success(f"{len(hits)} résultat(s) : voir l'onglet Catalogues, puis l'onglet Industriels.")


# ---------------------------------------------------------------------------
# Onglet Catalogues
# ---------------------------------------------------------------------------


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
        render_internal_hit(hit, today, client, store, settings)
    entries = [(st.session_state.get("decisions", {}).get(f"int|{h.key}", ("", {}))[0] or "", row_from_internal(h, today)) for h in hits]
    st.download_button("Exporter ces résultats (CSV)", to_csv_bytes(entries), "resultats_catalogues.csv", "text/csv", width="stretch")


def decision_key(hit: InternalHit) -> str:
    return f"int|{hit.supplier}|{hit.catalog}|{hit.original_page}|{hit.content_hash}"


def render_internal_hit(hit: InternalHit, today: str, client: CloudinaryClient | None, store: IndexStore, settings: Settings) -> None:
    with st.container(border=True):
        thumb_col, text_col, score_col = st.columns([1, 5, 1])
        if hit.thumb:
            thumb_col.image(hit.thumb, width=110)
        text_col.markdown(f"**{hit.title}**  \n{hit.supplier} · {hit.catalog} · {hit.page_label}")
        score_col.metric("Score", hit.score_label)
        facts = []
        if hit.refs:
            facts.append("Réf. " + ", ".join(hit.refs))
        if hit.weights_g:
            facts.append(", ".join(f"{w:g} g" for w in hit.weights_g))
        facts.extend(hit.packaging[:2])
        if hit.visual_similarity is not None:
            facts.append(f"similarité visuelle {hit.visual_similarity * 100:.0f} %")
        if facts:
            text_col.write(" · ".join(facts))
        text_col.caption(hit.excerpt)
        if hit.duplicates:
            text_col.caption(f"{hit.duplicates} passage(s) identique(s) regroupé(s).")
        with st.expander("Pourquoi ce score ?"):
            rows = [{"Composante": k, "Points": v} for k, v in hit.components.items()]
            rows += [{"Composante": f"pénalité : {k}", "Points": -v} for k, v in hit.penalties.items()]
            st.table(rows)
            for note in hit.notes:
                st.caption("• " + note)
        render_preview(hit, client, store, settings)
        decision_buttons("int", decision_key(hit), row_from_internal(hit, today))


def _show_preview(flag: str) -> None:
    st.session_state[flag] = True


def render_preview(hit: InternalHit, client: CloudinaryClient | None, store: IndexStore, settings: Settings) -> None:
    """Aperçu de la page d'origine, à la demande pour limiter les téléchargements."""
    if client is None or not pdf_extract.fitz_available():
        st.caption("Aperçu de la page indisponible (Cloudinary ou PyMuPDF non configuré).")
        return
    flag = f"preview-{hit.key}"
    st.button("Afficher l'aperçu de la page", key=f"btn-{flag}", on_click=_show_preview, args=(flag,))
    if st.session_state.get(flag):
        try:
            st.image(page_image(hit.key, hit.public_id, hit.local_page, client, store), width="stretch")
        except SourcingError as exc:
            st.warning(f"Aperçu indisponible : {redact(exc, settings.secret_values())}")


# ---------------------------------------------------------------------------
# Onglet Industriels (Internet)
# ---------------------------------------------------------------------------


def run_web(settings: Settings, store: IndexStore, plan: QueryPlan, options: WebSearchOptions, descriptor: ImageDescriptor | None,
            known: list[KnownSupplier] | None, priority: tuple[str, ...] = ()) -> WebSearchReport | None:
    cache = get_web_cache()
    names = tuple(sorted(s.name for s in known)) if known else ()
    digest = hashlib.sha1(descriptor.vector.tobytes()).hexdigest()[:12] if descriptor is not None else ""
    cache_key = (plan.cache_key(), options, names, digest, priority)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    engine = WebSourcing(TavilyClient(settings.tavily_api_key), manufacturers=load_verified_manufacturers(settings.manufacturers_path),
                         directory=known_directory(settings, store), options=options)
    bar = st.progress(0.0, text="Recherche Internet…")
    try:
        progress = lambda label, fraction: bar.progress(fraction, text=label)  # noqa: E731
        report = engine.run_known(plan, known, progress, descriptor) if known else engine.run(plan, progress, descriptor, priority)
    except SourcingError as exc:
        show_error(exc, settings)
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
    descriptor: ImageDescriptor | None = need.get("descriptor")
    st.caption("Seules des pages officielles du fabricant peuvent confirmer un fabricant. Rien n'est inventé : "
               f"« {NOT_FOUND_PUBLIC} » est affiché quand aucune preuve n'est trouvée.")
    if not settings.tavily_ready:
        st.info("Ajoutez TAVILY_API_KEY (clé « tvly-… ») dans les Secrets Streamlit pour activer la recherche Internet.")
        return
    mode = st.radio("Mode", ["Liste + découverte (recommandé)", "Qualifier mes fournisseurs choisis à la main"], horizontal=True)
    budget = st.slider("Budget de requêtes Tavily pour cette recherche (quota gratuit mensuel limité)", 4, 20, 12, 1)
    list_labels = {"Prioritaire : qualifier ma liste en premier, puis découvrir du nouveau": LIST_PRIORITY,
                   "Seulement repérer « connu / nouveau »": LIST_FLAG_ONLY, "Exclure ma liste (découverte pure)": LIST_EXCLUDE}
    share = 40
    list_mode = LIST_PRIORITY
    if mode.startswith("Liste"):
        list_mode = list_labels[st.radio("Rôle de ma liste de fournisseurs", list(list_labels), index=0)]
        if list_mode == LIST_PRIORITY:
            share = st.slider("Part du budget réservée à ma liste (le reste découvre de nouveaux industriels)", 10, 70, 40, 5, format="%d %%")
    c1, c2 = st.columns(2)
    use_base = c1.checkbox("Utiliser la base de fabricants vérifiés", value=True)
    extended = c2.checkbox("Europe élargie (Balkans, Turquie, Ukraine…)", value=False)
    options = WebSearchOptions(max_queries=budget, list_mode=list_mode, list_share=share / 100, europe_extended=extended, use_base=use_base)
    hits = st.session_state.get("hits") or []
    priority = tuple(dict.fromkeys(h.supplier for h in hits[:15] if h.supplier and h.supplier != "Fournisseur inconnu"))
    if priority and mode.startswith("Liste") and list_mode == LIST_PRIORITY:
        st.caption("Fournisseurs déjà vus dans vos catalogues pour ce besoin, qualifiés en premier : " + ", ".join(priority[:6]) + ("…" if len(priority) > 6 else ""))
    known: list[KnownSupplier] | None = None
    if not mode.startswith("Liste"):
        known = pick_known_suppliers(settings, store)
        label = "Qualifier les fournisseurs sélectionnés"
    else:
        label = "Rechercher des industriels"
    if st.button(label, type="primary", width="stretch", disabled=(known is not None and not known)):
        st.session_state["web_report"] = run_web(settings, store, plan, options, descriptor, known, priority)
    report: WebSearchReport | None = st.session_state.get("web_report")
    if report is None:
        return
    if report.list_size:
        st.caption(f"Votre liste ({report.list_size} fournisseurs) : {report.list_qualified} qualifié(s) · "
                   f"{report.new_qualified} nouveau(x) industriel(s) découvert(s)")
    st.caption(f"{len(report.candidates)} candidat(s) · {report.queries_used} requête(s) Tavily · "
               f"{report.excluded_results} résultat(s) écarté(s) (marketplaces, recettes, réseaux sociaux, hors Europe…) · vérifié le {report.checked_on}")
    for warning in report.warnings[:5]:
        st.warning(redact(warning, settings.secret_values()))
    if report.unresolved:
        st.info("Site officiel introuvable pour : " + ", ".join(report.unresolved))
    render_web_transparency(report)
    shown = filter_candidates(report.candidates)
    for candidate in shown:
        render_candidate(candidate)
    if shown:
        st.download_button("Exporter ces industriels (CSV)", to_csv_bytes([("", row_from_candidate(c)) for c in shown]),
                           "industriels.csv", "text/csv", width="stretch")


def pick_known_suppliers(settings: Settings, store: IndexStore) -> list[KnownSupplier]:
    directory = known_directory(settings, store)
    suppliers = directory.suppliers
    cats = sorted({s.category for s in suppliers if s.category})
    chosen_cat = st.multiselect("Filtrer par catégorie", cats)
    pool = [s for s in suppliers if not chosen_cat or s.category in chosen_cat]
    names = st.multiselect(f"Fournisseurs à qualifier ({len(pool)} disponibles ; 1 à 2 requêtes chacun)", [s.name for s in pool])
    return [s for s in pool if s.name in names]


def render_web_transparency(report: WebSearchReport) -> None:
    with st.expander(f"Comment la recherche a été conduite ({len(report.queries)} requêtes)"):
        for query in report.queries:
            st.caption("• " + query)
        for reason, count in report.exclusion_reasons.items():
            st.caption(f"Écartés : {count} × {reason}")
    if report.known_skipped:
        with st.expander(f"{len(report.known_skipped)} fournisseur(s) déjà référencé(s) écarté(s) sans dépenser de requête"):
            for company, domain, origin in report.known_skipped:
                st.caption(f"• {company} ({domain}) → {origin}")


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
        if candidate.image_url:
            top.image(candidate.image_url, width=130, caption=f"similarité visuelle {((candidate.visual_similarity or 0) * 100):.0f} %")
        st.markdown(" · ".join([link_line("Page produit", candidate.product_page_url), link_line("Fiche / catalogue", candidate.datasheet_url),
                                link_line("Contact", candidate.contact_url)]))
        if candidate.similar_product:
            st.caption(f"Produit proche : {candidate.similar_product}")
        if candidate.weights_g:
            st.caption("Grammage trouvé : " + ", ".join(f"{w:g} g" for w in candidate.weights_g))
        st.caption(f"Preuve industrielle : {candidate.industrial_level.value} · produit : {candidate.product_level.value} · "
                   f"private label : {candidate.private_label.value} · foodservice : {candidate.foodservice.value}")
        with st.expander("Preuves, score et points à confirmer"):
            for evidence in [*candidate.industrial_evidence, *candidate.product_evidence]:
                st.markdown(f"- **{evidence.kind}** ({evidence.origin}, {evidence.checked_on or 'date non renseignée'}) : [{evidence.url}]({evidence.url})")
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
        decision_buttons("web", f"web|{candidate.domain}", row_from_candidate(candidate))


# ---------------------------------------------------------------------------
# Shortlist et bibliothèque
# ---------------------------------------------------------------------------


def tab_shortlist(settings: Settings) -> None:
    decisions: dict[str, tuple[str, dict[str, str]]] = st.session_state.get("decisions", {})
    if st.session_state.get("shortlist_notice"):
        st.caption(st.session_state["shortlist_notice"])
    if not decisions:
        st.info("Marquez des résultats « Pertinent » ou « À étudier » dans les onglets Catalogues et Industriels.")
        return
    st.caption("Shortlist partagée : toute personne ayant le lien de l'application voit les mêmes décisions (sauvegarde sur Cloudinary).")
    keep = st.multiselect("Décisions à exporter", list(DECISIONS), default=["Pertinent", "À étudier"])
    entries = [(label, row) for label, row in decisions.values() if label in keep]
    st.write(f"{len(entries)} ligne(s) sélectionnée(s) sur {len(decisions)}.")
    st.dataframe([{"Décision": d, "Par": r.get("décidé par", ""), "Origine": r["origine"], "Fournisseur": r["fournisseur"], "Produit": r["produit"],
                   "Statut": r["statut"], "Score": r["score (points)"], "Date": r["date de vérification"]} for d, r in entries],
                 width="stretch", hide_index=True)
    st.download_button("Télécharger la shortlist (CSV, Excel FR)", to_csv_bytes(entries),
                       file_name=f"shortlist_sourcing_{date.today().isoformat()}.csv", mime="text/csv",
                       disabled=not entries, width="stretch")
    left, right = st.columns(2)
    if left.button("💾 Sauvegarder maintenant", width="stretch"):
        message = save_shared_shortlist(settings, force=True)
        st.warning(message) if message else st.success("Shortlist sauvegardée sur Cloudinary.")
    if right.button("Vider la shortlist", width="stretch"):
        st.session_state["removed"] = set(decisions)
        st.session_state["decisions"] = {}
        st.session_state["dirty"] = True
        st.rerun()


def tab_library(store: IndexStore) -> None:
    records = store.list_files()
    if not records:
        st.info("Aucun catalogue synchronisé.")
        return
    labels = {"indexed": "OK", "needs_ocr": "OCR requis", "error": "Erreur"}
    st.caption("Les PDF découpés en parties sont regroupés sous leur nom de catalogue ; « Pages d'origine » montre la plage reconstituée.")
    st.dataframe(
        [{"Fournisseur": r.supplier, "Catalogue": r.logical_name.rsplit("/", 1)[-1],
          "Partie": f"{r.part_index}/{r.part_total}" if r.part_index and r.part_total else (str(r.part_index) if r.part_index else ""),
          "Pages d'origine": f"{r.page_offset + 1}–{r.page_offset + r.page_count}" if r.page_count else "",
          "Taille Mo": round((r.size_bytes or 0) / 1024 / 1024, 2), "Passages": r.passages, "Photos": r.images,
          "Statut": labels.get(r.status, r.status) + (f" : {r.status_detail}" if r.status != "indexed" and r.status_detail else "")}
         for r in records],
        width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def password_gate(settings: Settings) -> bool:
    """Mot de passe facultatif (secret APP_PASSWORD). Sans secret, l'application est ouverte à qui a le lien."""
    if not settings.app_password or st.session_state.get("authed"):
        return True
    st.title("🔎 AI Sourcing Délifrance")
    entered = st.text_input("Mot de passe", type="password")
    if st.button("Entrer", type="primary"):
        if hmac.compare_digest(entered.encode(), settings.app_password.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Mot de passe incorrect.")
    return False


def run_app(settings: Settings) -> None:
    store = get_store(str(settings.db_path))
    restore_once(settings, store)
    load_shared_shortlist(settings)
    st.sidebar.text_input("Votre prénom (shortlist partagée)", key="user_name")
    status_box = st.sidebar.container()
    sidebar_sync(settings, store)
    sidebar_upload(settings)
    sidebar_status(status_box, settings, store)
    need_tab, result_tab, web_tab, shortlist_tab, library_tab = st.tabs(
        ["🎯 Besoin", "📁 Catalogues", "🌍 Industriels", "✅ Shortlist", "📚 Bibliothèque"])
    with need_tab:
        tab_need(settings, store)
    with result_tab:
        tab_catalogues(settings, store)
    with web_tab:
        tab_web(settings, store)
    with shortlist_tab:
        tab_shortlist(settings)
    with library_tab:
        tab_library(store)
    notice = save_shared_shortlist(settings)
    if notice:
        st.sidebar.warning(notice)
    st.divider()
    st.caption("Outil d'aide au sourcing pour l'acheteur tiers Délifrance. Vérifier chaque information avant contact.")


def main() -> None:
    st.set_page_config(page_title="AI Sourcing Délifrance", page_icon="🔎", layout="wide")
    settings = load_settings()
    if not password_gate(settings):
        return
    st.title("🔎 AI Sourcing Délifrance")
    st.caption("Catalogues Cloudinary · Recherche texte et photo · Sourcing industriel européen")
    try:
        run_app(settings)
    except SourcingError as exc:
        show_error(exc, settings)
    except Exception as exc:  # noqa: BLE001 - dernier filet : aucun secret ne doit fuiter dans un message technique
        st.error(f"Erreur inattendue ({type(exc).__name__}). Les détails techniques sont masqués pour protéger les secrets.")
        with st.expander("Détails techniques (secrets masqués)"):
            st.code(redact(traceback.format_exc(), settings.secret_values()))


if __name__ == "__main__":
    main()
