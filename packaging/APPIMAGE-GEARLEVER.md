# AppImage + Gear Lever — guía práctica (medida, no teorizada)

Fuente primaria: [AppImageSpec draft](https://raw.githubusercontent.com/AppImage/AppImageSpec/master/draft.md)
(`gh-releases-zsync|user|repo|latest|patron`). Fuente secundaria: código de
Gear Lever leído en el flatpak (`AppImageProvider.py`, `models/GithubUpdater.py`,
`AppDetails.py`, `lib/ini_config.py`). Todo lo de abajo se verificó contra
releases reales v1.1.0→v1.2.1 del proyecto RU5600.

## 1. Lo que la spec exige (tipo 2, el que usa appimagetool)

- ELF válido con filesystem adentro; update-info en la sección ELF **`.upd_info`**.
- Formato pipe, 5 campos, SIN URL:
  `gh-releases-zsync|Jeanjo1310|RU5600-usb-app|latest|ru5600-*-x86_64.AppImage.zsync`
  appimagetool **rechaza** el formato `gh-releases-zsync://...` (solo pipe).
- Nombre recomendado: `App-Version-Arq.AppImage` (`ru5600-1.2.1-x86_64.AppImage`).
- Adentro: `AppRun`, UN `$APPNAME.desktop`, `.DirIcon` (PNG 256), iconos en
  `usr/share/icons/hicolor/` + `pixmaps` (algunos DE solo miran ahí).

## 2. Las 3 claves del .desktop (no son spec, son convención Gear Lever)

| Clave | Para qué sirve (verificado en código) |
|---|---|
| `X-AppImage-UpdateInformation` (formato URL `gh-releases-zsync://...`) | Autofill del formulario al integrar |
| `X-AppImage-Version=1.2.1` | **ÚNICO** lugar de donde sale la versión instalada (`desktop_entry.get('X-AppImage-Version','')`, si falta muestra vacío). Estampar en el build desde `packaging/VERSION`, nunca a mano |
| `X-AppImage-Name` | Nombre mostrado |

## 3. Cómo decide Gear Lever (tres rutas distintas, no una)

- **Detectar**: lee `.upd_info` embebido + compara SHA-1 del `.zsync` remoto
  contra el hash local. El `.desktop` se ignora para esto.
- **Descargar**: usa la config **GUARDADA** (`gearlever.conf`, sección
  `[app.<md5-del-path>.update_manager]`), NO lo detectado. Sin guardar → `Missing target_asset`.
- **El dropdown en "Por defecto" BORRA lo guardado** (`AppDetails.py:701`
  `delete_app_update_config`). Por eso "recargar borra la fuente".

Formato de la sección guardada (copiar de una app que ande):
```
[app.<md5>.update_manager]
allow_prereleases = False
repo = Usuario/Repo
repo_filename = patron-*-x86_64.AppImage.zsync
manager = GithubUpdater
```

## 4. Ritual que funciona (en este orden)

1. Integrar el AppImage nuevo (regenera el `.desktop` con la versión).
2. Gestión de la actualización → Fuente **Github** → completar repo + patrón.
3. **Guardar** (✓) y verificar la sección en `gearlever.conf`.
4. Recargar metadatos → Actualizar (delta zsync, no los 33 MB).

## 5. Límites reales

- GitHub anónimo = **60 consultas/hora por IP** (Gear Lever gasta varias por
  chequeo). Sin token (Gear Lever **no soporta token**) el "no hay
  actualizaciones" puede ser rate-limit, no tu release. Verificar con
  `curl -s https://api.github.com/rate_limit` o `gh api rate_limit` (autenticado = 5000/h).
- Cada release: AppImage + `.zsync` (generado con
  `zsyncmake -u <url-del-release> archivo.AppImage`) + `latest.json` + `SHA256SUMS.txt`.
  El patrón del `.upd_info` tiene que matchear el nombre del asset (`*` = comodín).

## 6. Verificación antes de publicar (las 3 mandan)

```bash
readelf --string-dump=.upd_info --wide app.AppImage   # pipe gh-releases-zsync|... (5 campos)
./app.AppImage --appimage-extract *.desktop && grep -E "^X-AppImage-(Version|UpdateInformation)" squashfs-root/*.desktop
curl -sL <release>/latest/download/latest.json        # version + sha del binario nuevo
```
