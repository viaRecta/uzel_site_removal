"""Customer-facing PDF: what was found, with screenshots, and a Remove / Keep decision per item.

The PDF is a fillable form (radio buttons + text fields) that also works on paper. Items the
customer leaves unmarked mean "no action". Nothing is removed or requested by generating it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Image as RLImage,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .excel_io import (
    H_CATEGORY,
    H_CONFIDENCE,
    H_CONTACT,
    H_CONTENT,
    H_DATA_TYPES,
    H_DATE_FOUND,
    H_DOMAIN,
    H_EVIDENCE,
    H_FALLBACK,
    H_JURISDICTION,
    H_MENTIONS,
    H_METHOD,
    H_OWN,
    H_OWNER,
    H_PLACES,
    H_SEVERITY,
    H_STATUS,
    H_URL,
    Tracker,
)
from .identifiers import ClientIdentifiers

log = logging.getLogger(__name__)

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low"]
SEVERITY_COLORS = {
    "Critical": colors.HexColor("#B42318"),
    "High": colors.HexColor("#C4320A"),
    "Medium": colors.HexColor("#B54708"),
    "Low": colors.HexColor("#475467"),
}
INCLUDED_DEFAULT = {"Confirmed", "Likely"}
NAMESAKE = "Namesake (not client)"

FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]

# ---------------------------------------------------------------- text

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Online Personal Information Report",
        "prepared_for": "Prepared for",
        "case": "Case no.",
        "date": "Date",
        "prepared_by": "Prepared by",
        "confidential": "Confidential",
        "page": "Page",
        "intro1": "This report lists web pages where your personal information was found during our search "
                  "on {date}. <b>Nothing has been removed and no request has been sent to any website yet.</b>",
        "intro2": "Please review each item and mark <b>Remove</b> or <b>Keep</b>. We will only file removal "
                  "requests for items you mark <b>Remove</b> and authorize on the last page. Items left "
                  "unmarked are treated as <b>Keep</b>.",
        "intro3": "You can fill in this PDF on screen (Adobe Acrobat Reader, Microsoft Edge, Chrome) or print it "
                  "and mark it by hand. Screenshots show each page as it looked when we checked it.",
        "summary": "Summary",
        "total": "Items in this report",
        "by_severity": "By importance",
        "by_category": "By type of website",
        "count": "Count",
        "item": "Item",
        "ref": "Reference",
        "list_title": "List of findings",
        "list_intro": "Item numbers match the ID column of our tracking spreadsheet. Click a number to go to "
                      "that item's page with the screenshot and your Remove / Keep choice.",
        "no": "No.",
        "action_short": "Proposed action",
        "link": "Link",
        "site": "Website",
        "owner": "Site owner",
        "country": "Country",
        "site_type": "Type of website",
        "content": "Content type",
        "exposed": "Information shown",
        "where": "Where your name appears",
        "mentions": "Times your name appears",
        "own": "Your own account",
        "confidence": "Match",
        "action": "Proposed action",
        "fallback": "Also (search engine)",
        "contact": "Removal page / contact",
        "status": "Current status",
        "found": "Found on",
        "severity": "Importance",
        "decision": "Your decision:",
        "remove": "Remove",
        "keep": "Keep",
        "comment": "Comment:",
        "no_shot": "No screenshot available (the site does not allow automated access, or the page could not be loaded).",
        "cropped": "Top part of the page shown.",
        "auth_title": "Removal Authorization",
        "auth_text": "I have reviewed the items in this report. I ask that removal be requested on my behalf for "
                     "every item I marked <b>Remove</b>, and that no action be taken for items marked <b>Keep</b> "
                     "or left unmarked.",
        "auth_list": "Items in this report",
        "name": "Full name",
        "signature": "Signature",
        "sign_date": "Date",
        "none": "—",
        "empty": "No items to report.",
        "disclaimer": "Proposed actions are general suggestions, not legal advice.",
    },
    "tr": {
        "title": "Çevrimiçi Kişisel Bilgi Raporu",
        "prepared_for": "Hazırlanan kişi",
        "case": "Dosya no",
        "date": "Tarih",
        "prepared_by": "Hazırlayan",
        "confidential": "Gizli",
        "page": "Sayfa",
        "intro1": "Bu rapor, {date} tarihinde yaptığımız aramada kişisel bilgilerinizin yer aldığı internet "
                  "sayfalarını listeler. <b>Henüz hiçbir içerik kaldırılmadı ve hiçbir siteye talep gönderilmedi.</b>",
        "intro2": "Lütfen her maddeyi inceleyip <b>Kaldır</b> veya <b>Kalsın</b> seçeneğini işaretleyin. Kaldırma "
                  "taleplerini yalnızca <b>Kaldır</b> olarak işaretlediğiniz ve son sayfada onayladığınız maddeler "
                  "için başlatacağız. İşaretlenmeyen maddeler <b>Kalsın</b> olarak kabul edilir.",
        "intro3": "Bu PDF'i ekranda doldurabilir (Adobe Acrobat Reader, Microsoft Edge, Chrome) veya yazdırıp elle "
                  "işaretleyebilirsiniz. Ekran görüntüleri, sayfaların kontrol ettiğimiz andaki halini gösterir.",
        "summary": "Özet",
        "total": "Rapordaki madde sayısı",
        "by_severity": "Önem derecesine göre",
        "by_category": "Site türüne göre",
        "count": "Adet",
        "item": "Madde",
        "ref": "Referans",
        "list_title": "Bulgu listesi",
        "list_intro": "Madde numaraları, takip tablomuzdaki ID sütunuyla aynıdır. Bir numaraya tıklayarak o maddenin "
                      "ekran görüntüsünü ve Kaldır / Kalsın seçimini içeren sayfasına gidebilirsiniz.",
        "no": "No",
        "action_short": "Önerilen işlem",
        "link": "Bağlantı",
        "site": "Site",
        "owner": "Site sahibi",
        "country": "Ülke",
        "site_type": "Site türü",
        "content": "İçerik türü",
        "exposed": "Görünen bilgiler",
        "where": "Adınızın geçtiği yerler",
        "mentions": "Adınızın geçme sayısı",
        "own": "Size ait hesap",
        "confidence": "Eşleşme",
        "action": "Önerilen işlem",
        "fallback": "Ek olarak (arama motoru)",
        "contact": "Kaldırma sayfası / iletişim",
        "status": "Güncel durum",
        "found": "Bulunma tarihi",
        "severity": "Önem",
        "decision": "Kararınız:",
        "remove": "Kaldır",
        "keep": "Kalsın",
        "comment": "Notunuz:",
        "no_shot": "Ekran görüntüsü yok (site otomatik erişime izin vermiyor veya sayfa yüklenemedi).",
        "cropped": "Sayfanın üst kısmı gösterilmiştir.",
        "auth_title": "Kaldırma Onayı",
        "auth_text": "Bu rapordaki maddeleri inceledim. <b>Kaldır</b> olarak işaretlediğim her madde için benim "
                     "adıma kaldırma talebinde bulunulmasını, <b>Kalsın</b> olarak işaretlediğim veya "
                     "işaretlemediğim maddeler için herhangi bir işlem yapılmamasını istiyorum.",
        "auth_list": "Rapordaki maddeler",
        "name": "Ad Soyad",
        "signature": "İmza",
        "sign_date": "Tarih",
        "none": "—",
        "empty": "Raporlanacak madde yok.",
        "disclaimer": "Önerilen işlemler genel önerilerdir, hukuki görüş değildir.",
    },
}

VALUES_TR: dict[str, str] = {
    # Severity
    "Critical": "Kritik", "High": "Yüksek", "Medium": "Orta", "Low": "Düşük",
    # Site category
    "Data broker / people search": "Kişi arama / veri simsarı sitesi", "News / media": "Haber / medya",
    "Social media": "Sosyal medya", "Forum / community": "Forum / topluluk", "Blog": "Blog",
    "Business directory": "Firma rehberi", "Government / court record": "Resmî kayıt / mahkeme kaydı",
    "Professional network": "Profesyonel ağ", "Image / video host": "Görsel / video sitesi",
    "Archive / cache": "Arşiv / önbellek", "Review site": "Şikâyet / yorum sitesi", "Other": "Diğer",
    # Removal method
    "Client deletes own account": "Hesabı kendiniz silersiniz",
    "Site opt-out form": "Sitenin kaldırma formu",
    "Direct request to site owner": "Site sahibine doğrudan talep",
    "KVKK request (Law 6698)": "KVKK başvurusu (6698 sayılı Kanun)",
    "GDPR Art. 17 erasure request": "GDPR 17. madde silme talebi",
    "Google 'Results about you' / personal info form": "Google 'Hakkınızdaki sonuçlar' / kişisel bilgi kaldırma formu",
    "Google Outdated Content tool": "Google Güncel Olmayan İçerik aracı",
    "Court application (Law 5651)": "Mahkeme başvurusu (5651 sayılı Kanun)",
    "Lawyer / legal notice": "Avukat / ihtarname",
    "Hosting provider abuse report": "Barındırma sağlayıcısına şikâyet",
    "No action (namesake / low value)": "İşlem gerekmiyor",
    # Status
    "Not started": "Başlanmadı", "Awaiting client input": "Onayınız bekleniyor", "Requested": "Talep gönderildi",
    "Pending / in review": "İnceleniyor", "Removed": "Kaldırıldı",
    "Deindexed only": "Yalnızca arama sonuçlarından kaldırıldı", "Refused": "Reddedildi",
    "Re-appeared": "Yeniden yayında", "No action needed": "İşlem gerekmiyor",
    # Yes / No
    "Yes": "Evet", "No": "Hayır", "Unknown": "Bilinmiyor",
    # Match confidence
    "Confirmed": "Doğrulandı", "Likely": "Muhtemel", "Unverified": "Henüz doğrulanmadı",
    # Content type
    "Text": "Metin", "Image": "Görsel", "Video": "Video", "PDF / document": "PDF / belge",
    "Profile page": "Profil sayfası", "Listing": "Kayıt / ilan", "Comment": "Yorum", "Cached copy": "Önbellek kopyası",
    # Data types
    "Full name": "Ad soyad", "Phone": "Telefon", "Email": "E-posta", "Address": "Adres",
    "Date of birth": "Doğum tarihi", "TC Kimlik No": "TC kimlik no", "IBAN / financial": "IBAN / finansal bilgi",
    "Photo": "Fotoğraf", "Relatives": "Akrabalar", "Employer": "İşveren", "School": "Okul", "City": "Şehir",
    "Username": "Kullanıcı adı",
    # Mentioned places
    "Title": "Sayfa başlığı", "Meta description": "Arama açıklaması", "Headings": "Başlıklar",
    "Body": "Sayfa metni", "Image alt/captions": "Görsel açıklamaları", "Comments section": "Yorumlar",
    "URL slug": "Sayfa adresi", "Search snippet": "Arama sonucu özeti",
}


def tr(value: Any, lang: str) -> str:
    s = "" if value is None else str(value).strip()
    return VALUES_TR.get(s, s) if lang == "tr" else s


def tr_list(value: Any, lang: str) -> str:
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return ", ".join(tr(p, lang) for p in parts)


# ---------------------------------------------------------------- data


@dataclass
class ReportItem:
    id: int
    row: int
    values: dict[str, Any]
    evidence: Path | None = None

    def get(self, header: str) -> Any:
        v = self.values.get(header)
        return "" if v is None else v


@dataclass
class ReportMeta:
    client_name: str
    case_id: str
    date: dt.date
    prepared_by: str = ""
    lang: str = "en"
    extra: dict[str, Any] = field(default_factory=dict)


def select_items(tracker: Tracker, include_unverified: bool = False, order: str = "id") -> list[ReportItem]:
    """Rows for the customer: Confirmed + Likely (and Unverified if asked). Never namesakes.

    Item numbers in the report are the tracker IDs. order="id" keeps the spreadsheet order;
    order="severity" puts Critical first (IDs still match the spreadsheet)."""
    wanted = INCLUDED_DEFAULT | ({"Unverified"} if include_unverified else set())
    base = tracker.path.parent
    items = []
    for r in tracker.existing_rows():
        confidence = str(r.values.get(H_CONFIDENCE) or "").strip()
        if confidence == NAMESAKE or confidence not in wanted:
            continue
        evidence = None
        ev = str(r.values.get(H_EVIDENCE) or "").strip()
        if ev:
            p = Path(ev)
            p = p if p.is_absolute() else base / p
            evidence = p if p.exists() else None
        items.append(ReportItem(id=r.id, row=r.row, values=r.values, evidence=evidence))
    if order == "severity":
        rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        items.sort(key=lambda it: (rank.get(str(it.get(H_SEVERITY)), 9), it.id))
    else:
        items.sort(key=lambda it: it.id)
    return items


# ---------------------------------------------------------------- fonts & styles


def register_fonts(regular: str | None = None, bold: str | None = None) -> tuple[str, str]:
    """Register a TTF font that covers Turkish characters (ş, ğ, ı, İ). The PDF's built-in
    Helvetica does not, so a system font is required."""
    candidates = [(regular, bold or regular)] if regular else FONT_CANDIDATES
    for reg, bld in candidates:
        if reg and Path(reg).exists():
            pdfmetrics.registerFont(TTFont("Report", reg))
            pdfmetrics.registerFont(TTFont("Report-Bold", bld if bld and Path(bld).exists() else reg))
            pdfmetrics.registerFontFamily("Report", normal="Report", bold="Report-Bold",
                                          italic="Report", boldItalic="Report-Bold")
            return "Report", "Report-Bold"
    raise RuntimeError(
        "No font with Turkish characters found. Set report.font (and report.font_bold) in config.yaml "
        "to a .ttf file such as Arial or DejaVuSans."
    )


def _styles(font: str, bold: str) -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=font, fontSize=9.5, leading=13, alignment=TA_LEFT,
                          textColor=colors.HexColor("#1D2939"))
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=colors.HexColor("#475467")),
        "label": ParagraphStyle("label", parent=base, fontName=bold, fontSize=8.5, leading=11,
                                textColor=colors.HexColor("#475467")),
        "title": ParagraphStyle("title", parent=base, fontName=bold, fontSize=22, leading=28, spaceAfter=6),
        "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=15, leading=20, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=4),
        "link": ParagraphStyle("link", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#1849A9")),
        "badge": ParagraphStyle("badge", parent=base, fontName=bold, fontSize=9, leading=11, textColor=colors.white),
    }


def _esc(s: Any) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, style: ParagraphStyle) -> Paragraph:
    u = _esc(url)
    # Zero-width break opportunities so long URLs wrap.
    shown = u.replace("/", "/\u200b").replace("-", "-\u200b").replace("?", "?\u200b").replace("&amp;", "&amp;\u200b")
    return Paragraph(f'<link href="{u}">{shown}</link>', style)


# ---------------------------------------------------------------- form flowables


class DecisionBox(Flowable):
    """Remove / Keep radio buttons plus a comment field (fillable; also visible when printed)."""

    def __init__(self, item_id: int, text: dict[str, str], font: str, bold: str, width: float):
        super().__init__()
        self.item_id = item_id
        self.text = text
        self.font, self.bold = font, bold
        self.width, self.height = width, 16 * mm

    def draw(self) -> None:
        c = self.canv
        c.setStrokeColor(colors.HexColor("#D0D5DD"))
        c.setFillColor(colors.HexColor("#F9FAFB"))
        c.roundRect(0, 0, self.width, self.height, 4, stroke=1, fill=1)
        y = self.height - 7 * mm
        c.setFillColor(colors.HexColor("#1D2939"))
        c.setFont(self.bold, 10)
        c.drawString(4 * mm, y, self.text["decision"])
        x = 4 * mm + c.stringWidth(self.text["decision"], self.bold, 10) + 6 * mm
        form = c.acroForm
        name = f"decision_{self.item_id}"
        for value in ("remove", "keep"):
            form.radio(name=name, value=value, selected=False, x=x, y=y - 1.2 * mm, size=4.5 * mm,
                       buttonStyle="check", borderColor=colors.HexColor("#344054"), fillColor=colors.white,
                       textColor=colors.HexColor("#101828"), forceBorder=True, relative=True,
                       tooltip=self.text[value])
            c.setFont(self.bold, 10)
            c.drawString(x + 6.5 * mm, y, self.text[value])
            x += 6.5 * mm + c.stringWidth(self.text[value], self.bold, 10) + 10 * mm
        c.setFont(self.font, 9)
        c.drawString(4 * mm, 3.2 * mm, self.text["comment"])
        cx = 4 * mm + c.stringWidth(self.text["comment"], self.font, 9) + 3 * mm
        c.setStrokeColor(colors.HexColor("#98A2B3"))
        c.line(cx, 2 * mm, self.width - 4 * mm, 2 * mm)  # writing line on paper
        form.textfield(name=f"comment_{self.item_id}", x=cx, y=2 * mm, width=self.width - cx - 4 * mm,
                       height=5.5 * mm, relative=True, borderWidth=0, fillColor=colors.HexColor("#F9FAFB"), fontName="Helvetica", fontSize=9, tooltip=self.text["comment"])


class SignatureFields(Flowable):
    def __init__(self, text: dict[str, str], font: str, width: float):
        super().__init__()
        self.text, self.font = text, font
        self.width, self.height = width, 42 * mm

    def draw(self) -> None:
        c = self.canv
        form = c.acroForm
        y = self.height - 8 * mm
        for key in ("name", "signature", "sign_date"):
            c.setFont(self.font, 10)
            c.setFillColor(colors.HexColor("#1D2939"))
            c.drawString(0, y + 1.5 * mm, self.text[key])
            c.setStrokeColor(colors.HexColor("#667085"))
            c.line(35 * mm, y, self.width, y)
            if key != "signature":  # handwritten or e-signed; a text box would invite typing a name
                form.textfield(name=f"auth_{key}", x=35 * mm, y=y + 0.5 * mm, width=self.width - 35 * mm,
                               height=6 * mm, relative=True, borderWidth=0, fillColor=colors.white,
                               fontName="Helvetica", fontSize=10, tooltip=self.text[key])
            y -= 13 * mm


# ---------------------------------------------------------------- images


def screenshot_jpeg(path: Path, max_aspect: float) -> tuple[BytesIO, int, int, bool]:
    """Top part of a (possibly very tall) full-page screenshot as compact JPEG.
    `max_aspect` = max height / width kept. Returns (jpeg, width_px, height_px, cropped)."""
    Image.MAX_IMAGE_PIXELS = 200_000_000
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        keep_h = min(h, int(w * max_aspect))
        cropped = keep_h < h
        im = im.crop((0, 0, w, keep_h))
        if w > 1400:
            im = im.resize((1400, int(keep_h * 1400 / w)))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=75, optimize=True)
        iw, ih = im.size
    buf.seek(0)
    return buf, iw, ih, cropped


def screenshot_image(path: Path, max_w: float, max_h: float) -> tuple[RLImage, bool]:
    buf, iw, ih, cropped = screenshot_jpeg(path, max_h / max_w)
    scale = min(max_w / iw, max_h / ih)
    img = RLImage(buf, width=iw * scale, height=ih * scale)
    return img, cropped


# ---------------------------------------------------------------- document


def _fmt_date(v: Any, lang: str) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%d.%m.%Y" if lang == "tr" else "%Y-%m-%d")
    return str(v or "")


def build_pdf(
    items: list[ReportItem],
    meta: ReportMeta,
    out_path: Path,
    font_regular: str | None = None,
    font_bold: str | None = None,
) -> Path:
    lang = meta.lang if meta.lang in TEXT else "en"
    T = TEXT[lang]
    font, bold = register_fonts(font_regular, font_bold)
    S = _styles(font, bold)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
        title=f"{T['title']} - {meta.case_id}", author=meta.prepared_by or "", subject=T["title"],
    )
    width = doc.width
    date_s = _fmt_date(meta.date, lang)

    def on_page(canvas, d):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(16 * mm, 9 * mm, f"{T['confidential']} · {meta.client_name} · {meta.case_id}")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"{T['page']} {d.page}")
        canvas.restoreState()

    story: list[Flowable] = []

    # Cover
    story += [Spacer(1, 18 * mm), Paragraph(_esc(T["title"]), S["title"]), Spacer(1, 4 * mm)]
    info = [[Paragraph(T["prepared_for"], S["label"]), Paragraph(_esc(meta.client_name), S["base"])],
            [Paragraph(T["case"], S["label"]), Paragraph(_esc(meta.case_id), S["base"])],
            [Paragraph(T["date"], S["label"]), Paragraph(date_s, S["base"])]]
    if meta.prepared_by:
        info.append([Paragraph(T["prepared_by"], S["label"]), Paragraph(_esc(meta.prepared_by), S["base"])])
    story.append(Table(info, colWidths=[38 * mm, width - 38 * mm],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)])))
    story.append(Spacer(1, 8 * mm))
    for k in ("intro1", "intro2", "intro3"):
        story += [Paragraph(T[k].format(date=date_s), S["base"]), Spacer(1, 3 * mm)]

    story.append(Paragraph(T["summary"], S["h2"]))
    story.append(Paragraph(f"{T['total']}: <b>{len(items)}</b>", S["base"]))
    story.append(Spacer(1, 3 * mm))
    if items:
        sev_counts = [(s, sum(1 for i in items if i.get(H_SEVERITY) == s)) for s in SEVERITY_ORDER]
        cats: dict[str, int] = {}
        for i in items:
            cats[str(i.get(H_CATEGORY) or "Other")] = cats.get(str(i.get(H_CATEGORY) or "Other"), 0) + 1
        sev_rows = [[Paragraph(T["by_severity"], S["label"]), Paragraph(T["count"], S["label"])]] + [
            [Paragraph(f'<font color="#{SEVERITY_COLORS[s].hexval()[2:]}"><b>{tr(s, lang)}</b></font>', S["base"]),
             Paragraph(str(n), S["base"])] for s, n in sev_counts if n
        ]
        cat_rows = [[Paragraph(T["by_category"], S["label"]), Paragraph(T["count"], S["label"])]] + [
            [Paragraph(_esc(tr(c, lang)), S["base"]), Paragraph(str(n), S["base"])]
            for c, n in sorted(cats.items(), key=lambda x: -x[1])
        ]
        grid = TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#98A2B3")),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#EAECF0")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")])
        half = (width - 8 * mm) / 2
        story.append(Table([[Table(sev_rows, colWidths=[half - 18 * mm, 18 * mm], style=grid),
                             Table(cat_rows, colWidths=[half - 18 * mm, 18 * mm], style=grid)]],
                           colWidths=[half + 4 * mm, half + 4 * mm],
                           style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])))
    else:
        story.append(Paragraph(T["empty"], S["base"]))
    story += [Spacer(1, 6 * mm), Paragraph(T["disclaimer"], S["small"])]

    # One page per item
    for it in items:
        story.append(PageBreak())
        sev = str(it.get(H_SEVERITY) or "")
        badge = Table([[Paragraph(_esc(tr(sev, lang)) or T["none"], S["badge"])]],
                      style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), SEVERITY_COLORS.get(sev, colors.grey)),
                                        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
        head = Table([[Paragraph(f"{T['item']} {it.id} · {_esc(it.get(H_DOMAIN))}", S["h1"]), badge]],
                     colWidths=[width - 32 * mm, 32 * mm],
                     style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(head)
        story.append(Spacer(1, 2 * mm))

        def row(label: str, value: Any, is_link: bool = False):
            if value in (None, ""):
                value_p = Paragraph(T["none"], S["base"])
            elif is_link:
                value_p = _link(str(value), S["link"])
            else:
                value_p = Paragraph(_esc(value), S["base"])
            return [Paragraph(label, S["label"]), value_p]

        mentions = it.get(H_MENTIONS)
        fallback = it.get(H_FALLBACK)
        rows = [
            row(T["link"], it.get(H_URL), is_link=True),
            row(T["site"], it.get(H_DOMAIN)),
            row(T["owner"], it.get(H_OWNER)),
            row(T["country"], it.get(H_JURISDICTION)),
            row(T["site_type"], tr(it.get(H_CATEGORY), lang)),
            row(T["content"], tr(it.get(H_CONTENT), lang)),
            row(T["exposed"], tr_list(it.get(H_DATA_TYPES), lang)),
            row(T["where"], tr_list(it.get(H_PLACES), lang)),
        ]
        if mentions not in (None, ""):
            rows.append(row(T["mentions"], mentions))
        rows += [
            row(T["own"], tr(it.get(H_OWN), lang)),
            row(T["confidence"], tr(it.get(H_CONFIDENCE), lang)),
            row(T["action"], tr(it.get(H_METHOD), lang)),
        ]
        if fallback:
            rows.append(row(T["fallback"], tr(fallback, lang)))
        if it.get(H_CONTACT):
            rows.append(row(T["contact"], it.get(H_CONTACT), is_link=True))
        rows += [row(T["status"], tr(it.get(H_STATUS), lang)), row(T["found"], _fmt_date(it.get(H_DATE_FOUND), lang))]
        story.append(Table(rows, colWidths=[42 * mm, width - 42 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#EAECF0")),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ])))
        story.append(Spacer(1, 3 * mm))
        story.append(DecisionBox(it.id, T, font, bold, width))
        story.append(Spacer(1, 4 * mm))

        if it.evidence is not None:
            try:
                img, cropped = screenshot_image(it.evidence, width - 2, 120 * mm)
                framed = Table([[img]], style=TableStyle([
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#98A2B3")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))
                framed.hAlign = "LEFT"
                parts: list[Flowable] = [framed]
                if cropped:
                    parts.append(Paragraph(T["cropped"], S["small"]))
                story.append(KeepTogether(parts))
            except Exception as exc:  # corrupt / unreadable image: report it, don't fail the PDF
                log.warning("Could not embed screenshot for #%s: %s", it.id, type(exc).__name__)
                story.append(Paragraph(T["no_shot"], S["small"]))
        else:
            story.append(Paragraph(T["no_shot"], S["small"]))

    # Authorization page
    story.append(PageBreak())
    story.append(Paragraph(T["auth_title"], S["h1"]))
    story += [Paragraph(T["auth_text"], S["base"]), Spacer(1, 5 * mm), Paragraph(T["auth_list"], S["h2"])]
    if items:
        lst = [[Paragraph(T["item"], S["label"]), Paragraph(T["site"], S["label"]), Paragraph(T["severity"], S["label"])]]
        lst += [[Paragraph(str(it.id), S["base"]), Paragraph(_esc(it.get(H_DOMAIN)), S["base"]),
                 Paragraph(_esc(tr(it.get(H_SEVERITY), lang)), S["base"])] for it in items]
        story.append(Table(lst, colWidths=[28 * mm, width - 58 * mm, 30 * mm], repeatRows=1, style=TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#98A2B3")),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#EAECF0")),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ])))
    else:
        story.append(Paragraph(T["empty"], S["base"]))
    story += [Spacer(1, 12 * mm), SignatureFields(T, font, width * 0.8)]

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return out_path


def generate(
    tracker_path: Path,
    ident: ClientIdentifiers,
    out_path: Path,
    *,
    lang: str = "en",
    include_unverified: bool = False,
    order: str = "id",
    prepared_by: str = "",
    font_regular: str | None = None,
    font_bold: str | None = None,
    today: dt.date | None = None,
) -> tuple[Path, int]:
    tracker = Tracker(tracker_path)
    items = select_items(tracker, include_unverified, order)
    meta = ReportMeta(client_name=ident.full_name, case_id=ident.case_id, date=today or dt.date.today(),
                      prepared_by=prepared_by, lang=lang)
    build_pdf(items, meta, out_path, font_regular, font_bold)
    return out_path, len(items)
