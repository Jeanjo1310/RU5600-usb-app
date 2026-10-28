# Vista Patrones EPC — spec de diseño (v1, 2026-10-28)

## Contexto
El análisis de padrones EPC (estructuras, duplicados, choques, fórmulas)
hoy se hace a mano en Excel. Se lleva a la app como vista "Patrones".
Restricción dura: TODO offline y sin IA (corre dentro del AppImage).

## Alcance
- Entrada: UN solo archivo .xlsx/.csv (columna EPC
  autodetectada: encabezados conocidos, si no escaneo regex hex 12+,
  si no desplegable manual). Sin TID por el momento.
- Sin padrones: la familia se auto-detecta por prefijo del EPC.
- Salida: tabla + resumen + informe xlsx descargable.

## Reglas de análisis (deterministas, ~150 líneas)
1. Columnas derivadas: W34, card/tag id/short en UNA columna conmutable
   (conmutador global HEX↔DECIMAL en la vista, no dos columnas juntas).
   W34 = pos 7-14; card = su decimal 10 dígitos; tag id = pos 17-24;
   short = pos 19-24. En modo HEX se muestra el corte, en modo DECIMAL
   su valor (card con 10 dígitos, TAG ID ZKTECO con 8).
   Chip: solo familia probable por encabezado EPC, marcada (candidato).
2. Patrones: familias por prefijo; marcador fijo = tramo con <5 valores
   distintos; serial = todo distinto; rangos mín/máx; secuencialidad.
3. Novedades: duplicados internos; raros (largo≠24, ceros, no-hex);
   no-encajantes → balde "irregulares" (igual pasan por choques).
4. Choques: intersección EPC y W34 numérico (con y sin ceros) contra
   ambos padrones. Veredicto: compatible / lista de choques.

## UI y flujo
- Pestaña "Patrones": dropzone del archivo a analizar (único).
- Resultado: tabla con conmutador HEX/DECIMAL tipo interruptor
  (por defecto DECIMAL) + resumen estructurado (familias con conteos,
  marcadores por tramo, duplicados y rarezas clicables que filtran
  la tabla; filas duplicadas en rojo, rarezas en naranja) + buscador
  de tabla + botón Descargar informe (datos + OBSERVACIONES + ejemplos).
- Errores: sin columna EPC detectable → aviso + selector manual.

## Testing
- Analizador contra los 3 archivos reales (1000, ZKTECO, 400 leídos):
  debe replicar los valores ya medidos (W34 únicos, choques 0,
  marcadores 2B/20, duplicado id 400, 800 ceros, 313 ausente).
- AST + import check por cada cambio (regla del repo).

## Fuera de alcance (por ahora)
TID/chip exacto, escritura de EPC, comparación contra barrido en vivo.
