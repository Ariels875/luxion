"""
core/
=====

Capa de logica de negocio de Luxion. Contiene TODA la logica real del
sistema: base de datos, interaccion con bspwm/X11, reconciliacion de
workspaces, lanzamiento de apps, gestion de atajos de teclado y el lock
de concurrencia.

REGLA DE ARQUITECTURA (no negociable, ver seccion 4.17 del plan
arquitectonico): ningun archivo dentro de este paquete debe importar Gtk,
Vte, ni ningun modulo de la capa ui/. Esto es lo que permite que
luxion_cli.py (invocado por sxhkd en cada pulsacion de un atajo de
teclado) responda rapido, sin pagar el costo de inicializar un toolkit
grafico completo solo para ejecutar logica que no lo necesita.

Tanto luxion_cli.py como luxion_gui.py (a traves de ui/) deben consumir
esta capa exclusivamente a traves de core.workspace_service, que actua
como fachada unica sobre el resto de los modulos internos.
"""
