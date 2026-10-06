import io
import re
from pathlib import Path

import cloudinary.uploader
import fitz

# Keep a safety margin below Cloudinary Free raw-file limit (10 MiB).
MAX_CLOUDINARY_RAW_BYTES = 9 * 1024 * 1024


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _optimized_pdf_bytes(document):
    return document.tobytes(garbage=4, deflate=True, clean=True)


def _rasterized_page_pdf(source_page, max_bytes=MAX_CLOUDINARY_RAW_BYTES):
    """Create a smaller one-page PDF when one source page alone is oversized."""
    for zoom, quality in ((1.25, 70), (1.0, 60), (0.8, 48), (0.65, 40)):
        pixmap = source_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        jpeg = pixmap.tobytes("jpeg", jpg_quality=quality)
        output = fitz.open()
        width = source_page.rect.width
        height = source_page.rect.height
        page = output.new_page(width=width, height=height)
        page.insert_image(page.rect, stream=jpeg)
        data = _optimized_pdf_bytes(output)
        output.close()
        if len(data) <= max_bytes:
            return data
    raise ValueError("Une page du PDF reste superieure a 9 Mo apres compression.")


def split_pdf_bytes(pdf_bytes, max_bytes=MAX_CLOUDINARY_RAW_BYTES):
    """Split a PDF into sub-9-MiB PDFs and return bytes with original page ranges."""
    source = fitz.open(stream=pdf_bytes, filetype="pdf")
    chunks = []
    current = fitz.open()
    current_start = 1

    for page_index in range(source.page_count):
        trial = fitz.open()
        if current.page_count:
            trial.insert_pdf(current)
        trial.insert_pdf(source, from_page=page_index, to_page=page_index)
        trial_bytes = _optimized_pdf_bytes(trial)

        if len(trial_bytes) <= max_bytes:
            current.close()
            current = trial
            continue

        trial.close()
        if current.page_count:
            chunks.append({
                "bytes": _optimized_pdf_bytes(current),
                "page_start": current_start,
                "page_end": page_index,
            })
            current.close()
            current = fitz.open()
            current_start = page_index + 1

        single = fitz.open()
        single.insert_pdf(source, from_page=page_index, to_page=page_index)
        single_bytes = _optimized_pdf_bytes(single)
        single.close()

        if len(single_bytes) > max_bytes:
            single_bytes = _rasterized_page_pdf(source.load_page(page_index), max_bytes)

        single_doc = fitz.open(stream=single_bytes, filetype="pdf")
        current.insert_pdf(single_doc)
        single_doc.close()

    if current.page_count:
        chunks.append({
            "bytes": _optimized_pdf_bytes(current),
            "page_start": current_start,
            "page_end": source.page_count,
        })

    current.close()
    source.close()
    return chunks


def upload_pdf_to_cloudinary(uploaded_file, target_folder):
    """Compress/split one Streamlit UploadedFile, then upload all parts as raw PDFs."""
    original_name = Path(uploaded_file.name).stem
    safe_stem = _safe_name(original_name)
    chunks = split_pdf_bytes(uploaded_file.getvalue())
    uploaded = []
    total = len(chunks)

    for number, chunk in enumerate(chunks, start=1):
        suffix = f"_part_{number:02d}_pages_{chunk['page_start']}-{chunk['page_end']}"
        public_id = f"{target_folder}/{safe_stem}{suffix}.pdf"
        result = cloudinary.uploader.upload(
            io.BytesIO(chunk["bytes"]),
            resource_type="raw",
            type="upload",
            public_id=public_id,
            overwrite=True,
            invalidate=True,
            tags=["ai-sourcing", "catalogue-fournisseur", safe_stem],
            context={
                "original_filename": uploaded_file.name,
                "part": f"{number}/{total}",
                "original_pages": f"{chunk['page_start']}-{chunk['page_end']}",
            },
        )
        uploaded.append({
            "public_id": result.get("public_id", public_id),
            "part": number,
            "total_parts": total,
            "page_start": chunk["page_start"],
            "page_end": chunk["page_end"],
            "size_mb": round(len(chunk["bytes"]) / 1024 / 1024, 2),
        })

    return uploaded
