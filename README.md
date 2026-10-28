# AGROX Live — actualización diaria de precios

Este paquete publica AGROX como sitio web estático en GitHub Pages y actualiza `data/prices.json` mediante GitHub Actions.

## Qué hace

- Revisa y actualiza las fuentes cada día a las **08:17 de Argentina** (11:17 UTC), además de permitir ejecución manual.
- Actualiza `data/prices.json` y conserva datos históricos, fecha, fuente, URL y calidad.
- Calcula variación anual, devaluación y variación real solo cuando hay observaciones comparables. Si no hay una serie comparable, mantiene el último dato y lo marca.
- Prueba el JSON, corre tests y valida la ejecución de la interfaz antes de publicar.
- Publica el HTML y el JSON actualizados **en la misma ejecución**. Así se evita depender de que el commit automático de los datos dispare otro workflow de publicación.
- La interfaz publicada vuelve a consultar los datos periódicamente y al regresar a la pestaña.

**Importante:** abrir el HTML con doble clic (`file://`) no activa actualizaciones programadas. El horario diario requiere que el proyecto esté en GitHub y GitHub Pages esté habilitado con **Source = GitHub Actions**.

## Publicación inicial — recomendación: GitHub Desktop

### 1. Descargar y descomprimir

Descargá `AGROX_Live_Actualizacion_Diaria.zip` y descomprimilo. Entrá en la carpeta `agrox-live` que aparece dentro. Ahí tienen que verse `index.html`, `data`, `scripts`, `tests`, `requirements.txt` y la carpeta `.github`.

### 2. Crear un repositorio en GitHub

1. Entrá en https://github.com/new e iniciá sesión.
2. En **Repository name**, escribí `agrox-live`.
3. Elegí **Public** si usás GitHub Free y querés que cualquiera pueda abrir la web. La app, los datos de precios y el código se publicarán públicamente.
4. No marques las opciones para agregar README, `.gitignore` ni licencia: creá el repositorio vacío con **Create repository**.

### 3. Clonar el repositorio con GitHub Desktop

1. Instalá GitHub Desktop desde https://desktop.github.com/ e iniciá sesión con la misma cuenta de GitHub.
2. En GitHub Desktop, elegí **File → Clone repository**.
3. Seleccioná el repositorio `agrox-live` y elegí dónde guardarlo en tu computadora.
4. Tocá **Clone**.

### 4. Copiar los archivos de AGROX

1. Abrí en el Explorador de Windows la carpeta `agrox-live` que salió del ZIP.
2. Copiá **todo su contenido** dentro de la carpeta clonada del paso anterior. Si Windows pregunta, aceptá combinar carpetas.
3. Confirmá que la carpeta del repositorio tenga directamente estos elementos: `index.html`, `data/prices.json`, `scripts/update_prices.py`, `tests/` y `.github/workflows/update-agrox-prices.yml`.
4. Importante: no dejes una carpeta `agrox-live` dentro de otra carpeta `agrox-live`; `index.html` y `.github` deben estar en la raíz del repositorio.
5. En GitHub Desktop, en **Changes**, comprobá que aparezcan los archivos. En Summary escribí `Publicar AGROX Live` y tocá **Commit to main**.
6. Tocá **Push origin** para subirlos a GitHub.

### 5. Habilitar la publicación web

1. Abrí el repositorio `agrox-live` en github.com.
2. Entrá en **Settings → Pages**.
3. En **Build and deployment → Source**, elegí **GitHub Actions**. No elijas `Deploy from a branch`: este paquete publica con un workflow propio.

### 6. Autorizar escritura de datos

1. Entrá en **Settings → Actions → General**.
2. Buscá **Workflow permissions**.
3. Si el repositorio o la organización lo permiten, seleccioná **Read and write permissions** y guardá.
4. El workflow ya pide `contents: write` para guardar el histórico actualizado. Si la organización lo bloquea, un administrador deberá autorizarlo.

### 7. Ejecutar la primera actualización

1. En el repositorio, entrá en **Actions**.
2. Seleccioná el workflow **Actualizar precios AGROX**.
3. Tocá **Run workflow** y elegí la rama `main`.
4. Confirmá con el botón verde **Run workflow**.
5. Esperá a que la ejecución termine con un tilde verde. Abrí la ejecución y verificá que pasen los pasos de actualización, tests y publicación. Si falla, entrá en el paso rojo para leer el error.

### 8. Abrir la aplicación

1. Volvé a **Settings → Pages**.
2. Abrí el enlace que aparece como **Visit site**. La dirección suele ser `https://TU-USUARIO.github.io/agrox-live/`.
3. Para comprobar que los datos están publicados, agregá `data/prices.json` al final de la dirección base. Debe abrirse un JSON con la fecha de actualización y los elementos.

## Cómo funciona después

- El workflow se ejecuta a diario, alrededor de las 08:17 de Argentina. GitHub puede retrasar los trabajos programados durante períodos de carga alta.
- También corre al subir cambios a `main` y permite ejecución manual desde **Actions**.
- Si una fuente falla o no publica un dato nuevo, el proceso conserva el último dato válido y la fecha real de esa observación.
- Cada mercado conserva su frecuencia de publicación. Una revisión diaria no implica que todos los precios cambien todos los días.
- Los conectores que necesitan credenciales pueden usar un secreto de repositorio llamado `CONSIGNATARIAS_API_KEY`; el workflow funciona sin él y conserva los datos ante errores de autenticación.

## Solución de problemas

**No aparece el workflow en Actions:** verificá que subiste `.github/workflows/update-agrox-prices.yml` a la raíz del repositorio y que el commit llegó a `main`.

**El sitio muestra error 404:** verificá que `Settings → Pages → Source` esté en `GitHub Actions` y revisá que el job `deploy` haya terminado con tilde verde.

**El workflow falla al hacer `git push`:** revisá `Settings → Actions → General → Workflow permissions` y la política de la organización.

**La web abre pero muestra datos viejos:** en Actions comprobá la última ejecución; revisá también que el endpoint `data/prices.json` publicado tenga la fecha esperada. Los errores de una fuente no reemplazan el dato anterior por un valor inventado.

## Estructura

```text
agrox-live/
├── index.html
├── AGROX_Premium_v2_Datos_Historicos_Sensibilidad.html
├── data/prices.json
├── scripts/update_prices.py
├── tests/
├── requirements.txt
├── .nojekyll
└── .github/workflows/update-agrox-prices.yml
```

## Ejecución local opcional

Requiere Python 3.12+ y conexión a Internet:

```bash
pip install -r requirements.txt
python scripts/update_prices.py
python -m unittest discover -s tests -v
node tests/runtime_smoke.cjs
```

Para probar la web localmente y permitir `fetch('./data/prices.json')`, ejecutá desde la raíz:

```bash
python -m http.server 8000
```

Luego abrí http://localhost:8000.

## Fuentes y metodología

Las fuentes y las limitaciones por producto se documentan en el actualizador y en los campos de `data/prices.json`. No se completan cotizaciones faltantes con datos inventados ni se confunden automáticamente benchmarks internacionales con precios locales.
