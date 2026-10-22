"""Idiomas de la interfaz del cliente del RU5600.

La app se construye con los textos en ingles y despues se recorre el
arbol de widgets para ponerlos en el idioma elegido, asi que agregar
un idioma es agregar una tabla y un valor en ``LANGUAGES``.

``STRINGS`` traduce las etiquetas (pestanas, marcos, botones, campos).
``TOOLTIPS`` agrega la explicacion de cada control: es la unica ayuda
que tiene la app, asi que conviene que sea util.
"""
from __future__ import annotations

LANGUAGES = (("English", "en"), ("Espanol", "es"))

DEFAULT_LANG = "en"

# etiqueta -> {"en": ..., "es": ...}
STRINGS = {
    # --- pestanas -------------------------------------------------------
    "ParameterSet": {"en": "ParameterSet", "es": "Parametros"},
    "Read/Write": {"en": "Read/Write", "es": "Leer/Escribir"},
    "ActiveMode": {"en": "ActiveMode", "es": "Lectura continua"},
    "AdvanceSet": {"en": "AdvanceSet", "es": "Avanzado"},
    "Chip info": {"en": "Chip info", "es": "Info del chip"},
    "Risky": {"en": "Risky", "es": "Peligroso"},
    "WhiteList": {"en": "WhiteList", "es": "Lista blanca"},
    "BatchWrite": {"en": "BatchWrite", "es": "Escritura en lote"},
    # --- conexion -------------------------------------------------------
    "Connection": {"en": "Connection", "es": "Conexion"},
    "Open": {"en": "Open", "es": "Abrir"},
    "Close": {"en": "Close", "es": "Cerrar"},
    "Install USB access rule": {
        "en": "Install USB access rule",
        "es": "Instalar regla de acceso USB"},
    "Device": {"en": "Device", "es": "Dispositivo"},
    "DeviceSN:": {"en": "DeviceSN:", "es": "N. de serie:"},
    "Firmware:": {"en": "Firmware:", "es": "Firmware:"},
    "Hardware:": {"en": "Hardware:", "es": "Hardware:"},
    "Baudrate:": {"en": "Baudrate:", "es": "Velocidad:"},
    "Device:": {"en": "Device:", "es": "Puerto:"},
    "Interface:": {"en": "Interface:", "es": "Transporte:"},
    "WorkMode:": {"en": "WorkMode:", "es": "Modo del lector:"},
    "Read mode:": {"en": "Read mode:", "es": "Modo de lectura:"},
    "Net IP:": {"en": "Net IP:", "es": "IP de red:"},
    "NetPort:": {"en": "NetPort:", "es": "Puerto de red:"},
    # --- parametros -----------------------------------------------------
    "Reader parameters": {"en": "Reader parameters",
                          "es": "Parametros del lector"},
    "Reading settings": {"en": "Reading settings",
                         "es": "Ajustes de lectura"},
    "Keyboard": {"en": "Keyboard", "es": "Teclado"},
    "Band:": {"en": "Band:", "es": "Banda:"},
    "FreqBand:": {"en": "FreqBand:", "es": "Banda de frecuencia:"},
    "RFPower:": {"en": "RFPower:", "es": "Potencia RF:"},
    "FilterTime(S):": {"en": "FilterTime(S):", "es": "Filtro (s):"},
    "InquiryArea:": {"en": "InquiryArea:", "es": "Area de consulta:"},
    "StartAddress(Hex):": {"en": "StartAddress(Hex):",
                           "es": "Direccion inicial (hex):"},
    "ByteLength(Hex):": {"en": "ByteLength(Hex):", "es": "Longitud (hex):"},
    "ReadTime(*10ms):": {"en": "ReadTime(*10ms):", "es": "Tiempo (x10ms):"},
    "Buzzer": {"en": "Buzzer", "es": "Zumbador"},
    "Silence it": {"en": "Silence it", "es": "Silenciar"},
    "Mode:": {"en": "Mode:", "es": "Modo:"},
    "OutPutFormat:": {"en": "OutPutFormat:", "es": "Formato de salida:"},
    "FastWriteEPC:": {"en": "FastWriteEPC:", "es": "EPC rapida:"},
    "OutWithEnterKey": {"en": "OutWithEnterKey", "es": "Salir con Enter"},
    "SingleFreq": {"en": "SingleFreq", "es": "Frecuencia unica"},
    "Skin": {"en": "Skin", "es": "Apariencia"},
    "Accion:": {"en": "Accion:", "es": "Accion:"},
    "Refresh": {"en": "Refresh", "es": "Actualizar"},
    "ExportConfig": {"en": "ExportConfig", "es": "Exportar config"},
    "ImportConfig": {"en": "ImportConfig", "es": "Importar config"},
    # --- leer/escribir --------------------------------------------------
    "Read tag memory": {"en": "Read tag memory", "es": "Leer memoria del tag"},
    "Read memory": {"en": "Read memory", "es": "Leer memoria"},
    "Bank:": {"en": "Bank:", "es": "Banco:"},
    "WordPtr(Hex):": {"en": "WordPtr(Hex):", "es": "Palabra inicial (hex):"},
    "WordCount(Hex):": {"en": "WordCount(Hex):", "es": "Palabras (hex):"},
    "Password:": {"en": "Password:", "es": "Password:"},
    "Write EPC": {"en": "Write EPC", "es": "Escribir EPC"},
    "EPC (hex):": {"en": "EPC (hex):", "es": "EPC (hex):"},
    "Write memory (USER, EPC...)": {
        "en": "Write memory (USER, EPC...)",
        "es": "Escribir memoria (USER, EPC...)"},
    "Data(hex):": {"en": "Data(hex):", "es": "Datos (hex):"},
    "Write and verify": {"en": "Write and verify",
                         "es": "Escribir y verificar"},
    "Huella (anti-clonado)": {"en": "Huella (anti-clonado)",
                              "es": "Huella (anti-clonado)"},
    "Aprender huella del tag actual": {
        "en": "Learn the fingerprint of the current tag",
        "es": "Aprender huella del tag actual"},
    "Verificar la huella antes de escribir": {
        "en": "Verify the fingerprint before writing",
        "es": "Verificar la huella antes de escribir"},
    # --- lectura continua -----------------------------------------------
    "Measure": {"en": "Measure", "es": "Medir"},
    "Measure (s):": {"en": "Measure (s):", "es": "Medir (s):"},
    "Start read": {"en": "Start read", "es": "Empezar a leer"},
    "Stop read": {"en": "Stop read", "es": "Parar de leer"},
    "Status: stopped": {"en": "Status: stopped", "es": "Estado: parado"},
    "Status: reading": {"en": "Status: reading", "es": "Estado: leyendo"},
    "Ant": {"en": "Ant", "es": "Ant"},
    "EPC / Tag ID": {"en": "EPC / Tag ID", "es": "EPC / ID del tag"},
    "RSSI": {"en": "RSSI", "es": "RSSI"},
    "Count": {"en": "Count", "es": "Veces"},
    "Last seen": {"en": "Last seen", "es": "Ultima vez"},
    # --- info del chip ---------------------------------------------------
    "Analyze chip": {"en": "Analyze chip", "es": "Analizar chip"},
    "Test password": {"en": "Test password", "es": "Probar password"},
    "Pass:": {"en": "Pass:", "es": "Password:"},
    "Deep scan (memory map)": {"en": "Deep scan (memory map)",
                               "es": "Barrido profundo (mapa de memoria)"},
    # --- peligroso -------------------------------------------------------
    "Bloquear tag": {"en": "Lock tag", "es": "Bloquear tag"},
    "A - Bloquear tag (Gen2 block lock, cmd 0x05)": {
        "en": "A - Lock tag (Gen2 block lock, cmd 0x05)",
        "es": "A - Bloquear tag (block lock Gen2, cmd 0x05)"},
    "Lock data (4 bytes):": {"en": "Lock data (4 bytes):",
                              "es": "Datos del lock (4 bytes):"},
    "Lock the tag after writing (irreversible)": {
        "en": "Lock the tag after writing (irreversible)",
        "es": "Bloquear el tag despues de escribir (irreversible)"},
    "Entiendo el riesgo de estas operaciones": {
        "en": "I understand the risk of these operations",
        "es": "Entiendo el riesgo de estas operaciones"},
    "B - Comando crudo (CFHid_SendCommand)": {
        "en": "B - Raw command (CFHid_SendCommand)",
        "es": "B - Comando crudo (CFHid_SendCommand)"},
    "Comando:": {"en": "Command:", "es": "Comando:"},
    "Payload (hex):": {"en": "Payload (hex):", "es": "Payload (hex):"},
    "Enviar comando crudo": {"en": "Send raw command",
                             "es": "Enviar comando crudo"},
    "C - CryptWrite (EXPERIMENTAL, en desarrollo)": {
        "en": "C - CryptWrite (EXPERIMENTAL, in development)",
        "es": "C - CryptWrite (EXPERIMENTAL, en desarrollo)"},
    "Aplicar CryptWrite": {"en": "Apply CryptWrite",
                           "es": "Aplicar CryptWrite"},
    "Comandos conocidos del lector RU5600": {
        "en": "Known commands of the RU5600 reader",
        "es": "Comandos conocidos del lector RU5600"},
    # --- lista blanca ----------------------------------------------------
    "Entry:": {"en": "Entry:", "es": "Entrada:"},
    "Add": {"en": "Add", "es": "Agregar"},
    "Add one entry (32 bytes hex)": {
        "en": "Add one entry (32 bytes hex)",
        "es": "Agregar una entrada (32 bytes hex)"},
    "Delete one": {"en": "Delete one", "es": "Borrar una"},
    "Delete all": {"en": "Delete all", "es": "Borrar todas"},
    "Import": {"en": "Import", "es": "Importar"},
    "TotalCnt.:": {"en": "TotalCnt.:", "es": "Total:"},
    "entries: -": {"en": "entries: -", "es": "entradas: -"},
    # --- escritura en lote ----------------------------------------------
    "Write the same EPC to a batch of tags": {
        "en": "Write the same EPC to a batch of tags",
        "es": "Escribir el mismo EPC a un lote de tags"},
    "Verify after writing": {"en": "Verify after writing",
                             "es": "Verificar despues de escribir"},
    "Timeout (ms):": {"en": "Timeout (ms):", "es": "Timeout (ms):"},
    # --- avanzado --------------------------------------------------------
    "Frequency": {"en": "Frequency", "es": "Frecuencia"},
    "Read freq": {"en": "Read freq", "es": "Leer frecuencia"},
    "Set freq": {"en": "Set freq", "es": "Fijar frecuencia"},
    "Reboot reader": {"en": "Reboot reader", "es": "Reiniciar lector"},
    # --- varios ----------------------------------------------------------
    "Log": {"en": "Log", "es": "Registro"},
    "Language": {"en": "Language", "es": "Idioma"},
}

# etiqueta -> {"en": ..., "es": ...} con la explicacion del control
TOOLTIPS = {
    "Open": {
        "en": "Opens the reader over the transport chosen on the left. "
              "Only one program can hold the reader at a time.",
        "es": "Abre el lector con el transporte de la izquierda. Solo un "
              "programa puede tomar el lector a la vez."},
    "Close": {
        "en": "Closes the reader and leaves it quiet (it stops the "
              "continuous reading and goes back to answer mode).",
        "es": "Cierra el lector y lo deja quieto (para la lectura continua "
              "y vuelve al modo answer)."},
    "Install USB access rule": {
        "en": "Installs the udev rule that lets your user open the reader "
              "over USB without being root. Needs your password.",
        "es": "Instala la regla udev que permite abrir el lector por USB "
              "sin ser root. Pide tu password."},
    "Read memory": {
        "en": "Reads the chosen bank from the tag: 0 RESERVED, 1 EPC, "
              "2 TID, 3 USER. The word count is in 16 bit words.",
        "es": "Lee del tag el banco elegido: 0 RESERVED, 1 EPC, 2 TID, "
              "3 USER. La cantidad es en palabras de 16 bits."},
    "Write EPC": {
        "en": "Writes the EPC into the tag. The EPC is readable by anyone, "
              "so it is not a protection.",
        "es": "Escribe el EPC en el tag. El EPC lo puede leer cualquiera, "
              "no es una proteccion."},
    "Write and verify": {
        "en": "Writes the data and reads it back: what is reported as "
              "written was really read from the tag afterwards.",
        "es": "Escribe los datos y los vuelve a leer: lo que se reporta "
              "como escrito fue leido del tag despues."},
    "Analyze chip": {
        "en": "Reads the TID and the memory of the tag and says what chip "
              "it is and what is protected.",
        "es": "Lee el TID y la memoria del tag y dice que chip es y que "
              "esta protegido."},
    "Test password": {
        "en": "Checks the access password: if it is wrong the tag stops "
              "answering every read.",
        "es": "Prueba el access password: si esta mal, el tag deja de "
              "contestar todas las lecturas."},
    "Measure": {
        "en": "Reads for a few seconds and reports the detection rate and "
              "the antenna signal: the numbers to compare when mapping "
              "the reading area of the antenna.",
        "es": "Lee unos segundos y reporta la tasa de deteccion y la "
              "senal de la antena: los numeros para comparar al mapear "
              "el area de lectura."},
    "Start read": {
        "en": "Starts the continuous reading: the reader streams the tags "
              "it sees and each one gets a number of its own.",
        "es": "Arranca la lectura continua: el lector transmite los tags "
              "que ve y cada uno recibe su propio numero."},
    "Stop read": {
        "en": "Stops the continuous reading and leaves the reader in "
              "answer mode.",
        "es": "Para la lectura continua y deja el lector en modo answer."},
    "Silence it": {
        "en": "Turns the reader's buzzer off right away: it beeps on every "
              "tag it reads.",
        "es": "Apaga el zumbador del lector: pita con cada tag que lee."},
    "Buzzer": {
        "en": "The reader's buzzer. It is turned off when the reader is "
              "opened.",
        "es": "El zumbador del lector. Se apaga al abrir el lector."},
    "Read mode:": {
        "en": "How hard the reader tries on a single detection: Burst is "
              "one poll, Balanced three, Reliable keeps polling until "
              "the reader answers.",
        "es": "Cuanto insiste el lector en una deteccion: Burst es una "
              "consulta, Balanced tres, Reliable insiste hasta que el "
              "lector contesta."},
    "Bank:": {
        "en": "The memory bank: 0 RESERVED (passwords), 1 EPC, 2 TID, "
              "3 USER.",
        "es": "El banco de memoria: 0 RESERVED (passwords), 1 EPC, 2 TID, "
              "3 USER."},
    "WordPtr(Hex):": {
        "en": "Where the read starts, in words (2 bytes) from the "
              "beginning of the bank.",
        "es": "Donde empieza la lectura, en palabras (2 bytes) desde el "
              "principio del banco."},
    "WordCount(Hex):": {
        "en": "How many words (2 bytes) to read. The reader answers at "
              "most 8 words (16 bytes) per command.",
        "es": "Cuantas palabras (2 bytes) leer. El lector contesta como "
              "mucho 8 palabras (16 bytes) por comando."},
    "Password:": {
        "en": "The access password of the tag, 4 bytes in hex (00000000 "
              "when it has none).",
        "es": "El access password del tag, 4 bytes en hex (00000000 si no "
              "tiene)."},
    "EPC (hex):": {
        "en": "The new EPC, in hex. It has to be a multiple of 2 bytes.",
        "es": "El EPC nuevo, en hex. Tiene que ser multiplo de 2 bytes."},
    "Data(hex):": {
        "en": "The data to write, in hex, an even number of bytes.",
        "es": "Los datos a escribir, en hex, un numero par de bytes."},
    "FreqBand:": {
        "en": "The frequency band of the reader, by country.",
        "es": "La banda de frecuencia del lector, por pais."},
    "RFPower:": {
        "en": "The power of the antenna in dBm.",
        "es": "La potencia de la antena en dBm."},
    "FilterTime(S):": {
        "en": "How long the reader ignores a tag it just read.",
        "es": "Cuanto tiempo el lector ignora un tag que acaba de leer."},
    "ReadTime(*10ms):": {
        "en": "How long the reader reads a tag, in units of 10 ms.",
        "es": "Cuanto tiempo lee el lector un tag, en unidades de 10 ms."},
    "Lock the tag after writing (irreversible)": {
        "en": "Locks the tag for good. There is no way back: the EPC and "
              "the user memory become unchangeable forever.",
        "es": "Bloquea el tag para siempre. No hay vuelta atras: el EPC y "
              "la memoria de usuario quedan inmodificables."},
    "Entiendo el riesgo de estas operaciones": {
        "en": "Has to be ticked before the dangerous operations are "
              "enabled.",
        "es": "Hay que marcarlo para habilitar las operaciones "
              "peligrosas."},
    "Bloquear tag": {
        "en": "Locks the tag with the Gen2 block lock command (0x05). "
              "Irreversible.",
        "es": "Bloquea el tag con el comando block lock Gen2 (0x05). "
              "Irreversible."},
    "Enviar comando crudo": {
        "en": "Sends a raw command to the reader. The firmware update and "
              "the commands that kill a tag are blocked in the code.",
        "es": "Envia un comando crudo al lector. La actualizacion de "
              "firmware y los comandos que matan un tag estan bloqueados "
              "en el codigo."},
    "Comando:": {
        "en": "The command code in hex (for example 0x20 reads a "
              "parameter).",
        "es": "El codigo de comando en hex (por ejemplo 0x20 lee un "
              "parametro)."},
    "Payload (hex):": {
        "en": "The bytes that go with the command, in hex.",
        "es": "Los bytes que acompañan al comando, en hex."},
    "Reboot reader": {
        "en": "Restarts the reader. The reader answers almost nothing "
              "after a lot of commands and a restart brings it back. "
              "The reader reconnects by itself, so open it again after.",
        "es": "Reinicia el lector. Despues de muchas ordenes el lector "
              "casi no contesta y un reinicio lo revive. El lector se "
              "reconecta solo: despues hay que abrirlo de nuevo."},
    "Read freq": {
        "en": "Reads the frequency the reader is set to.",
        "es": "Lee la frecuencia a la que esta ajustado el lector."},
    "Set freq": {
        "en": "Sets the frequency of the reader.",
        "es": "Fija la frecuencia del lector."},
    "Net IP:": {
        "en": "The IP of the reader when it works over the network: the "
              "reader is the server.",
        "es": "La IP del lector cuando trabaja por red: el lector es el "
              "servidor."},
    "NetPort:": {
        "en": "The TCP port of the reader.",
        "es": "El puerto TCP del lector."},
    "Baudrate:": {
        "en": "The speed of the serial port, for a reader that works over "
              "a serial line.",
        "es": "La velocidad del puerto serie, para un lector que trabaja "
              "por linea serial."},
    "Interface:": {
        "en": "How the app talks to the reader: USB, a serial port or the "
              "network.",
        "es": "Como habla la app con el lector: USB, un puerto serie o la "
              "red."},
    "WorkMode:": {
        "en": "AnswerMode answers one command at a time; ActiveMode scans "
              "continuously and streams the tags. The memory reads answer "
              "much better in AnswerMode.",
        "es": "AnswerMode responde un comando a la vez; ActiveMode escanea "
              "seguido y transmite los tags. Las lecturas de memoria "
              "responden mucho mejor en AnswerMode."},
    "InquiryArea:": {
        "en": "Which antennas of the reader take part in the inventory.",
        "es": "Que antenas del lector participan en el inventario."},
    "StartAddress(Hex):": {
        "en": "Where the inventory starts, in hex.",
        "es": "Donde empieza el inventario, en hex."},
    "ByteLength(Hex):": {
        "en": "How many bytes the inventory covers, in hex.",
        "es": "Cuantos bytes cubre el inventario, en hex."},
    "Timeout (ms):": {
        "en": "How long the app waits for the reader on each tag.",
        "es": "Cuanto espera la app al lector en cada tag."},
    "TotalCnt.:": {
        "en": "How many tags the batch write writes to.",
        "es": "Cuantos tags escribe la escritura en lote."},
    "Verificar la huella antes de escribir": {
        "en": "Reads the TID of the tag and compares it with the one that "
              "was learned: it refuses to write on a tag that was "
              "cloned.",
        "es": "Lee el TID del tag y lo compara con el aprendido: se niega "
              "a escribir en un tag clonado."},
    "Aprender huella del tag actual": {
        "en": "Reads the TID of the tag in front of the reader and "
              "remembers it.",
        "es": "Lee el TID del tag que esta delante del lector y lo "
              "recuerda."},
    "Add one entry (32 bytes hex)": {
        "en": "Adds one entry to the white list: 32 bytes in hex.",
        "es": "Agrega una entrada a la lista blanca: 32 bytes en hex."},
    "Delete all": {
        "en": "Deletes every entry of the white list.",
        "es": "Borra todas las entradas de la lista blanca."},
    "Delete one": {
        "en": "Deletes the entry whose number is written in Entry.",
        "es": "Borra la entrada cuyo numero esta en Entrada."},
    "Import": {
        "en": "Loads the white list from a file.",
        "es": "Carga la lista blanca desde un archivo."},
    "Help": {
        "en": "Shows the manual of the app.",
        "es": "Muestra el manual de la app."},
    "CloseRelay": {
        "en": "Opens the relay of the reader.",
        "es": "Abre el rele del lector."},
    "ReleaseRelay": {
        "en": "Releases the relay of the reader.",
        "es": "Libera el rele del lector."},
    "ExportConfig": {
        "en": "Saves the reader configuration to a file.",
        "es": "Guarda la configuracion del lector en un archivo."},
    "ImportConfig": {
        "en": "Loads the reader configuration from a file.",
        "es": "Carga la configuracion del lector de un archivo."},
    "OutPutFormat:": {
        "en": "The format the reader uses to report the tags it reads.",
        "es": "El formato con que el lector reporta los tags que lee."},
    "FastWriteEPC:": {
        "en": "Writes the EPC without waiting for the tag to answer.",
        "es": "Escribe el EPC sin esperar a que el tag conteste."},
    "Mode:": {
        "en": "How the reader reports the tags it reads.",
        "es": "Como reporta el lector los tags que lee."},
    "Skin": {
        "en": "The look of the reader's own software.",
        "es": "La apariencia del software del lector."},
    "Accion:": {
        "en": "What the keyboard shortcuts do.",
        "es": "Que hacen los atajos de teclado."},
}


def _lang_code(lang) -> str:
    """Accepts 'es', 'Espanol' or a tuple."""
    if isinstance(lang, (tuple, list)):
        lang = lang[-1]
    text = str(lang).strip().lower()
    if text.startswith("es") or "espa" in text:
        return "es"
    return "en"


def tr(text: str, lang) -> str:
    """The translation of a visible text (the English one if unknown)."""
    entry = STRINGS.get(text)
    if not entry:
        return text
    return entry.get(_lang_code(lang), entry["en"])


def tip(text: str, lang) -> str:
    """The tooltip of a control (empty if it has none)."""
    entry = TOOLTIPS.get(text)
    if not entry:
        return ""
    return entry.get(_lang_code(lang), entry["en"])
