# Guía de interfaz — preferencias de UI (REA)

Base tomada de la interfaz web del RU5600 (Flask + DaisyUI), validada pantalla por
pantalla con el Creador. **Nivel actual: 7/10.** No es el techo: es el piso
aceptable para toda interfaz nueva. Si una pantalla nueva no cumple esta guía,
está por debajo del piso.

Stack de referencia: Flask + Jinja + Tailwind v4 + DaisyUI 5, **compilado local**
(`npx @tailwindcss/cli`), servido desde `static/css/bundle.css`.

---

## 1. Reglas duras (no negociables)

1. **Sin CDN.** Nada de `cdn.jsdelivr.net`, `@tailwindcss/browser` ni Google Fonts.
   Un CDN caído dejó todos los botones muertos (`tailwind is not defined`).
2. **Un solo acento.** El color de marca (índigo en claro, ámbar en oscuro) se usa
   en: acción principal, foco de campo, pestaña activa. Nada más.
3. **Verde solo para leer.** Todo botón que *lee* del hardware va en verde suave.
   Rojo solo para lo destructivo o irreversible.
4. **Un "peligro" no se ve como un botón normal.** Va con texto rojo de contraste
   (`.text-danger`), no con `text-error` (ese es color de fondo).
5. **Deshabilitado tiene que verse deshabilitado**: fondo del contenedor y texto al
   32%. Nunca el mismo aspecto que un control activo.
6. **Etiqueta arriba, control abajo.** Nunca label al costado, nunca `title=` como
   única documentación.
7. **El dato va en mono, la etiqueta no.** Valores de campo, EPC, hex, resultados de
   tabla → monoespaciada. Títulos, etiquetas y ayudas → fuente normal.
8. **Nada de comandos internos en pantalla.** Los códigos de protocolo (`0x2B`), los
   nombres del SDK (`CFHid_SendCommand`) y los catálogos de comandos no son UI:
   van al modal de ayuda como explicación de uso o no van.
9. **Tabla con su leyenda.** Si una columna no se explica sola (`Code W34`), el modal
   de ayuda la explica.
10. **Tema, idioma y cualquier preferencia se persisten** (`localStorage`) y se
    aplican en el `<head>`, antes del CSS, para que no haya flash.

---

## 2. Tokens

Copiar tal cual en `static/src/input.css`. Están medidos en navegador, no son
"aproximados".

```css
@import "tailwindcss";
@plugin "daisyui";

[data-theme="light"] {
  color-scheme: light;
  --color-primary: oklch(0.55 0.13 268);   --color-primary-content: oklch(0.98 0.01 268);
  --color-error:   oklch(0.85 0.07 18);    --color-error-content:   oklch(0.43 0.12 18);
  --color-warning: oklch(0.88 0.09 78);    --color-warning-content: oklch(0.40 0.09 65);
  --color-success: oklch(0.87 0.08 155);   --color-success-content: oklch(0.38 0.09 155);
  --color-info:    oklch(0.88 0.05 235);   --color-info-content:    oklch(0.38 0.08 240);
  --color-base-content: oklch(0.46 0.008 265);   /* gris oscuro, NO negro */
  --color-base-200: oklch(0.952 0.005 265);
  --color-base-300: oklch(0.902 0.007 265);
}

[data-theme="dark"] {
  color-scheme: dark;
  --color-base-100: oklch(0.192 0.006 258);      /* tarjetas */
  --color-base-200: oklch(0.152 0.006 258);      /* fondo */
  --color-base-300: oklch(0.275 0.008 258);      /* bordes */
  --color-base-content: oklch(0.930 0.003 258);
  --color-primary: oklch(0.800 0.120 70);        --color-primary-content: oklch(0.200 0.040 65);
  --color-success: oklch(0.800 0.130 155);       --color-success-content: oklch(0.180 0.045 155);
  --color-warning: oklch(0.840 0.120 78);        --color-warning-content: oklch(0.200 0.040 70);
  --color-error:   oklch(0.600 0.150 22);        --color-error-content:   oklch(0.980 0.010 22);
  --color-info:    oklch(0.740 0.090 240);       --color-info-content:    oklch(0.170 0.030 240);
  --radius-box: 0.875rem; --radius-field: 0.5rem; --radius-selector: 0.5rem;
  --border: 1px; --depth: 0; --noise: 0;
}
```

Campos (input / select / textarea):

```css
.input, .select, .textarea {
  background-color: color-mix(in oklab, var(--color-base-200) 40%, var(--color-base-100));
  border-color: var(--color-base-300);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  color: oklch(0.38 0.01 265);
}
[data-theme="dark"] .input, [data-theme="dark"] .select, [data-theme="dark"] .textarea {
  background-color: color-mix(in oklab, var(--color-base-200) 70%, var(--color-base-100));
  color: oklch(0.950 0.002 258);
}
```

Botón "Leer" (verde suave, texto legible — el `btn-soft` de DaisyUI deja el texto
del mismo verde del fondo, hay que pisarlo):

```css
.btn-soft.btn-success {
  background-color: oklch(0.945 0.045 155);
  border: 1px solid oklch(0.860 0.060 155);
  color: oklch(0.440 0.100 155);
  box-shadow: none;
}
[data-theme="dark"] .btn-soft.btn-success {
  background-color: oklch(0.300 0.045 155);
  border: 1px solid oklch(0.410 0.060 155);
  color: oklch(0.850 0.110 155);
}
```

Texto de riesgo: `.text-danger` → `oklch(0.52 0.16 20)` (claro) / `oklch(0.80 0.11 20)` (oscuro).

---

## 3. Layout

* Contenedor de página: `max-w-screen-xl mx-auto p-4`.
* **Tarjetas (`section`): `card bg-base-100 shadow-sm p-6`** — 24 px de padding.
  Borde hairline `1px` con `base-300`; sin sombra en tema oscuro.
* Campos: `grid grid-cols-1 md:grid-cols-3 gap-x-4 gap-y-3 items-start`.
  Filas parejas: una grilla, no una columna por grupo.
* Botones: `grid grid-cols-2 gap-2`; el ancho completo se reserva para la acción
  que aplica a toda la sección.
* Tablas: `table table-zebra w-full text-sm`, celdas `px-4 py-2`, contenedor
  `overflow-x-auto`.
* Nada de scroll horizontal en 1280 px: si aparece, la grilla está mal.

---

## 4. Componentes aprobados

| Componente | Cómo se hace |
|---|---|
| Navegación | `tabs tabs-border` con `border-b border-base-300`; activa por path, subrayado en el acento |
| Interruptores | `toggle toggle-sm toggle-primary` con las dos etiquetas a los lados; la activa en negrita, la otra al 50% |
| Botón de ayuda | `btn btn-outline btn-sm btn-square` con icono SVG, `title` + `aria-label`, atajo **F1** |
| Modal de ayuda | `<dialog class="modal">` + `modal-box max-w-2xl`, ítem = título semibold + texto al 75% |
| Aviso / ayuda en pantalla | `text-sm text-base-content/60` (nunca `text-gray-500`: no sigue el tema) |
| Estado / etiqueta | `badge badge-success|warning|error|info` |
| Alerta de riesgo | `alert alert-error` teñida: fondo `color-mix(error 22%, base-100)` + borde `error 50%` (oscuro) |
| Botón que lee | `btn btn-soft btn-success` |
| Acción principal | `btn btn-primary` |
| Acción destructiva | `btn btn-error`, nunca al lado de la principal sin separación |

---

## 5. Idioma (ES/EN)

* Diccionario `const STR = {"es":"en", ...}` en `base.html`.
* Un *walker* de nodos de texto traduce al cambiar, y recuerda el original por nodo.
  Funciona con texto suelto; **no** traduce texto generado por JS después del cambio
  (mensajes dinámicos quedan en el idioma de origen: aceptarlo o re-renderizar).
* Contenido de modales generado en JS: guardar pares `[es, en]` y elegir según `LANG`.
* Claves nuevas: agregar al diccionario en el mismo commit que el texto.

---

## 6. Checklist antes de dar una pantalla por terminada

- [ ] Etiquetas arriba de cada control, con `for`
- [ ] Todos los `<select>` con clase `select` (si no, pierden la flecha de listbox)
- [ ] Campos con relleno propio: no se confunden con la tarjeta
- [ ] Deshabilitados visibles y con la lógica correcta según el contexto
- [ ] Botones de lectura en verde; destructivos en rojo; principal único
- [ ] Ayuda con F1 y contenido real por campo (no repetir la etiqueta)
- [ ] ES/EN probado en los dos sentidos
- [ ] Tema claro y oscuro: contraste de texto ≥ 4.5:1 y ningún bloque claro huérfano
- [ ] Tema e idioma persisten al navegar y al recargar
- [ ] Sin scroll horizontal, sin ids duplicados, cero errores de consola
- [ ] Verificado con Playwright midiendo: alineaciones, colores computados, estados

---

## 7. Trampas ya pagadas (no repetir)

| Síntoma | Causa real |
|---|---|
| Ningún botón responde | CDN de Tailwind cargado después del JS → `tailwind is not defined` |
| Los cambios de plantilla no aparecen | Flask cachea Jinja: hay que reiniciar el proceso |
| Los radios "no funcionan" | Se probaba con `check()` (fuerza el estado); hay que clickear el `<label>` |
| Tema oscuro con inputs grises e ilegibles | Variables del tema claro declaradas en `:root`, que pisan las del oscuro. Van en `[data-theme="light"]` |
| `select` sin flecha | Usa `class="input"` en lugar de `class="select"` |
| Botón verde con texto invisible | `btn-soft` de DaisyUI usa el color de fondo como color de texto |
| Texto rojo ilegible | `text-error` es el color de *fondo*; para texto va `.text-danger` |
| Un botón hace otra cosa | Id duplicado entre base y plantilla (`#helpBtn`) |
| Tema que no persiste | `setTheme` sin `localStorage` y `<html data-theme>` fijo |
| Mensaje dinámico sin traducir | Texto generado por JS después del walker |

---

## 8. Del 7 al 9 (pendiente, no obligatorio)

1. **Estados vacíos**: hoy una tabla vacía es una franja gris. Debe decir qué hacer.
2. **Feedback de acción**: los mensajes van a un `<p>`; falta toast y estado "en curso".
3. **Foco visible**: el anillo de foco existe en campos, no en botones ni tabs.
4. **Responsive real < 640 px**: la grilla de 3 columnas colapsa, pero los botones de
   ancho fijo y las tablas necesitan revisión.
5. **Jerarquía**: todo pesa parecido (títulos, etiquetas y ayuda). Falta escala tipográfica.
6. **Iconografía**: solo el botón de ayuda tiene icono; el resto es texto.

---

*Origen: RU5600 web (`ru5600/web/`). Reutilizable por cualquier interfaz REA. Si se
convierte en estándar del ecosistema, ARCHI lo absorbe y esto queda como referencia.*
