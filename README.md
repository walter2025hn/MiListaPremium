# MiListaPremium

Lista M3U de Código Master.

## Actualización automática cada 6 horas

El workflow \`.github/workflows/update-playlist.yml\` combina hasta tres fuentes autorizadas, elimina duplicados por nombre y grupo, comprueba los enlaces y publica \`Completa.m3u\`.

### Configuración necesaria (una sola vez)

En GitHub abre **Settings → Secrets and variables → Actions → New repository secret** y crea estos tres secretos:

- \`M3U_SOURCE_1\`
- \`M3U_SOURCE_2\`
- \`M3U_SOURCE_3\`

En cada secreto pega la URL completa de una fuente M3U autorizada. No escribas esas URLs en archivos del repositorio ni en mensajes públicos.

Después abre **Actions → Actualizar lista Código Master → Run workflow** para ejecutar una prueba manual. Si todo está correcto, el workflow también se ejecutará automáticamente cada seis horas.

### Seguridad y protección

- Si falta una fuente o no se puede descargar, el proceso se detiene y conserva la lista publicada.
- Los enlaces que no responden tras dos intentos se reportan en \`reporte_enlaces.txt\`; si no hay una copia alternativa válida, se conservan para evitar borrar contenido por una caída temporal.
- Si el resultado contiene menos del 35% de las entradas actuales, no reemplaza la lista.
- El generador elimina encabezados M3U duplicados y separadores de texto, pero conserva los metadatos \`#EXTINF\` y las URLs.
- La dirección pública de \`Completa.m3u\` no cambia.

Las fuentes deben ser propias o contar con autorización de sus titulares.
