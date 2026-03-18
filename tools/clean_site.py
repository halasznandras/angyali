# tools/clean_site.py
# ------------------------------------------------------------------------------
# Használaton KÍVÜLI fájlok törlése + képek WEBP-re konvertálása + hivatkozás átírása
# HTML + CSS + (alapszinten) JS referenciafeltérképezéssel, OG-kép támogatással.
# Kimenet: clean_build.zip + REPORT.md + MANIFEST_BEFORE/AFTER.json
# ------------------------------------------------------------------------------
import os, re, shutil, zipfile, hashlib, json
from pathlib import Path
from bs4 import BeautifulSoup
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

# --- Beállítások (workflow-ból felülírhatók) ---
SITE_ROOT   = Path(os.getenv("SITE_ROOT", ".")).resolve()
WEBP_QUALITY= int(os.getenv("WEBP_QUALITY", "82"))
WEBP_METHOD = int(os.getenv("WEBP_METHOD", "6"))
KEEP_OG_JPG = os.getenv("KEEP_OG_JPG", "false").lower()=="true"  # ha true, meghagy 1 JPG og-image-t
OUTPUT_ZIP  = SITE_ROOT / "clean_build.zip"
BACKUP_DIR  = SITE_ROOT / "backup_originals"

HTML_EXT={".html",".htm"}; CSS_EXT={".css"}; JS_EXT={".js"}
IMG_EXT={".jpg",".jpeg",".png",".gif"}
EXCLUDE_DIRS={".git",".github","tools","backup_originals"}  # ezeket sose bántsa

RE_IMG_IN_JS = re.compile(r"""(['"])([\w\./\-@]+?\.(?:jpe?g|png|gif))\1""", re.I)
RE_URL_IN_CSS= re.compile(r"url\(([^)]+)\)")

def sha256(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def norm(base:Path, ref:str)->Path|None:
    ref = ref.split("?")[0].split("#")[0].strip()
    if not ref or ref.startswith(("http://","https://","//","data:","mailto:","tel:")): return None
    p = (SITE_ROOT/ref[1:]).resolve() if ref.startswith("/") else (base.parent/ref).resolve()
    try: p.relative_to(SITE_ROOT)
    except Exception: return None
    return p if p.exists() else None

def collect_refs():
    refs=set(); htmls=[]; csss=[]; jss=[]
    for p in SITE_ROOT.rglob("*"):
        if p.is_dir(): continue
        if any(x in p.parts for x in EXCLUDE_DIRS): continue
        ext=p.suffix.lower()
        if ext in HTML_EXT: htmls.append(p)
        elif ext in CSS_EXT: csss.append(p)
        elif ext in JS_EXT:  jss.append(p)

    # HTML
    for h in htmls:
        try: soup=BeautifulSoup(h.read_text(encoding="utf-8",errors="ignore"),"lxml")
        except: soup=BeautifulSoup(h.read_text(encoding="utf-8",errors="ignore"),"html.parser")
        for t in soup.find_all(["link","script","img","source","meta"]):
            for a in ("href","src"):
                if t.has_attr(a):
                    p=norm(h,str(t.get(a)));  refs.add(p) if p else None
            if t.name in ("img","source") and t.has_attr("srcset"):
                for e in str(t["srcset"]).split(","):
                    url=e.strip().split()[0]; p=norm(h,url); refs.add(p) if p else None
            if t.name=="meta" and t.get("property","")=="og:image" and t.get("content"):
                p=norm(h,t["content"]); refs.add(p) if p else None

    # CSS url(...)
    for c in csss:
        txt=c.read_text(encoding="utf-8",errors="ignore")
        for m in RE_URL_IN_CSS.findall(txt):
            url=m.strip().strip('"\''); p=norm(c,url); refs.add(p) if p else None

    # JS (best-effort)
    for j in jss:
        txt=j.read_text(encoding="utf-8",errors="ignore")
        for m in RE_IMG_IN_JS.finditer(txt):
            p=norm(j,m.group(2)); refs.add(p) if p else None

    # magukat a forrásokat is tartsuk
    for x in htmls+csss+jss: refs.add(x)
    return refs, htmls, csss, jss

def to_webp(src:Path)->Path|None:
    dst=src.with_suffix(".webp")
    try:
        im=Image.open(src)
        has_alpha=(im.mode in ("LA","RGBA")) or ("transparency" in im.info)
        kwargs={"method":WEBP_METHOD}
        if src.suffix.lower()==".png" and has_alpha:
            kwargs.update({"lossless":True,"quality":100})
        else:
            kwargs.update({"quality":WEBP_QUALITY})
        im.save(dst,format="WEBP",**kwargs)
        return dst if dst.exists() and dst.stat().st_size>0 else None
    except Exception:
        return None

def rewrite_html(h:Path, mapping:dict[Path,Path]):
    raw=h.read_text(encoding="utf-8",errors="ignore")
    soup=BeautifulSoup(raw,"lxml"); changed=False

    def repl(tag,attr):
        nonlocal changed
        if not tag.has_attr(attr): return
        val=str(tag.get(attr)); base=h
        if attr=="srcset":
            out=[]
            for part in val.split(","):
                toks=part.strip().split()
                if not toks: continue
                u=toks[0]; p=norm(base,u)
                if p and p in mapping:
                    toks[0]=str(mapping[p].relative_to(SITE_ROOT))
                    changed=True
                out.append(" ".join(toks))
            tag[attr]=", ".join(out); return
        p=norm(base,val)
        if p and p in mapping:
            tag[attr]=str(mapping[p].relative_to(SITE_ROOT)); changed=True

    for t in soup.find_all(True):
        if t.name in ("img","script","link","source"):
            for a in ("src","href","srcset"): repl(t,a)
        if t.name=="meta" and t.get("property","")=="og:image": repl(t,"content")

    if changed: h.write_text(str(soup),encoding="utf-8")

def rewrite_css(c:Path, mapping:dict[Path,Path]):
    txt=c.read_text(encoding="utf-8",errors="ignore")
    def r(m):
        u=m.group(1).strip().strip('"\''); p=norm(c,u)
        if p and p in mapping:
            new=str(mapping[p].relative_to(SITE_ROOT)).replace("\\","/")
            return f"url({new})"
        return m.group(0)
    out=RE_URL_IN_CSS.sub(r,txt)
    if out!=txt: c.write_text(out,encoding="utf-8")

def main():
    # Backup + manifest BEFORE
    if BACKUP_DIR.exists(): shutil.rmtree(BACKUP_DIR)
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    all_before=[p for p in SITE_ROOT.rglob("*")
                if p.is_file() and not any(x in p.parts for x in EXCLUDE_DIRS)]

    refs, htmls, csss, jss = collect_refs()
    used_imgs=[p for p in refs if p.suffix.lower() in IMG_EXT]
    mapping={}; converted=[]; failed=[]
    for img in used_imgs:
        dst=to_webp(img)
        if dst: mapping[img]=dst; converted.append((img,dst))
        else: failed.append(img)

    # átírás
    for h in htmls: rewrite_html(h,mapping)
    for c in csss:  rewrite_css(c,mapping)

    # eredetik mentése + törlése
    for s,d in converted:
        if s.exists():
            rel=s.relative_to(SITE_ROOT)
            (BACKUP_DIR/rel.parent).mkdir(parents=True,exist_ok=True)
            shutil.copy2(s,BACKUP_DIR/rel)
            if not KEEP_OG_JPG: s.unlink(missing_ok=True)

    # újrafelmérés és törlés mindarról, amire nincs hivatkozás
    refs_after,_,_,_ = collect_refs()
    keep=set(refs_after)
    removed=[]
    for p in SITE_ROOT.rglob("*"):
        if p.is_dir(): continue
        if any(x in p.parts for x in EXCLUDE_DIRS): continue
        if p==OUTPUT_ZIP or BACKUP_DIR in p.parents: continue
        if p not in keep:
            rel=p.relative_to(SITE_ROOT)
            (BACKUP_DIR/rel.parent).mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,BACKUP_DIR/rel)
            p.unlink(missing_ok=True)
            removed.append(rel.as_posix())

    # Jelentések
    def man(entries):
        return [{"path":str(p.relative_to(SITE_ROOT)).replace("\\","/"),
                 "size":p.stat().st_size,"sha256":sha256(p)} for p in entries]
    after_files=[p for p in SITE_ROOT.rglob("*")
                 if p.is_file() and BACKUP_DIR not in p.parents and p.name!="clean_build.zip"
                 and not any(x in p.parts for x in EXCLUDE_DIRS)]
    Path("MANIFEST_BEFORE.json").write_text(json.dumps(man(all_before),indent=2),encoding="utf-8")
    Path("MANIFEST_AFTER.json").write_text(json.dumps(man(after_files),indent=2),encoding="utf-8")

    # REPORT
    rep=[]
    rep.append("# Tisztítás + WEBP konverzió – Jelentés\n")
    rep.append("## Összegzés")
    rep.append(f"- Konvertált képek: **{len(converted)}**")
    rep.append(f"- Sikertelen konverziók: **{len(failed)}**")
    rep.append(f"- Eltávolított (nem használt) fájlok: **{len(removed)}**\n")
    if failed:
        rep.append("### Sikertelen képek:"); rep+=[f"- {p}" for p in failed]; rep.append("")
    if removed:
        rep.append("### Eltávolított fájlok (backup_originals/ alatt mentve):")
        rep+=[f"- {x}" for x in removed]; rep.append("")
    Path("REPORT.md").write_text("\n".join(rep),encoding="utf-8")

    # ZIP (csak szükséges fájlok)
    if OUTPUT_ZIP.exists(): OUTPUT_ZIP.unlink()
    with zipfile.ZipFile(OUTPUT_ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in SITE_ROOT.rglob("*"):
            if p.is_file() and BACKUP_DIR not in p.parents and p.name!=OUTPUT_ZIP.name and not any(x in p.parts for x in EXCLUDE_DIRS):
                z.write(p, p.relative_to(SITE_ROOT).as_posix())
    print("KÉSZ ✔  | clean_build.zip elkészült")

if __name__=="__main__":
    main()
