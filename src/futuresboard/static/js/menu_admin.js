// Menu Admin script extracted from base.html to external file to improve caching
const MenuAdmin = MA = {
    el: undefined,
    btns: undefined,
    respuesta: undefined,
    accesos: undefined,
    activar: () => {
        MA.el = document.getElementById('menu_admin');
        MA.btns = MA.el.querySelectorAll('[data-accion]');
        MA.respuesta = MA.el.querySelector('[name=respuesta]');
        MA.obtener_accesos(MA.activar_botones);
        MA.respuesta.addEventListener('click', MA.limpiar_aviso);
    },
    activar_botones: () => {
        let btn = undefined;
        for (btn of MA.btns) {
            MA.setear_boton(btn);
        }
    },
    setear_boton: (el) => {
        el.addEventListener('click', () => {
            let dat = {};
            dat['accion'] = el.getAttribute('data-accion');
            dat['url'] = MA.accesos.urls[dat['accion']];
            if (el.hasAttribute('data-input')) {
                let k = el.getAttribute('data-input');
                let v = el.parentNode.querySelector(`input[name=${k}]`).value;
                dat[k] = v;
            }
            MA.preparar_envio(dat);
        });
    },
    preparar_envio: (dat) => {
        let v = prompt('Por favor, ingrese su CLAVE DE USUARIO', '');
        if (v == MA.accesos.kac) {
            MA.enviar_solicitud(dat, MA.respuesta_ok, MA.respuesta_err);
        } else {
            console.log('ERROR clave incorrecta');
        }
    },
    obtener_accesos: (fn_ok = () => { }, fn_er = () => { }) => {
        let url = window.location.protocol + '//' + window.location.host + ':9009/slctdaeo';
        let xhr = new XMLHttpRequest();
        xhr.open('GET', url, true);
        xhr.send();
        xhr.onload = () => {
            if (xhr.status === 200) {
                fn_ok(xhr.response);
                MA.accesos = JSON.parse(xhr.response);
            } else {
                fn_er(xhr.response);
            }
        };
    },
    enviar_solicitud: (data, cb_ok = () => { }, cb_err = () => { }) => {
        let xhr = new XMLHttpRequest(),
            fd = new FormData();
        Object.keys(data).forEach(k => { if (k !== 'url') fd.append(k, data[k]); });
        xhr.open('POST', window.location.protocol + '//' + window.location.host + ':9009' + data.url, true);
        xhr.send(fd);
        xhr.onload = () => {
            if (xhr.status === 200) {
                cb_ok(xhr.response);
            } else {
                cb_err(xhr.response);
            }
        };
    },
    respuesta_ok: (d) => {
        d = JSON.parse(d);
        if (d.success) {
            MA.aviso('bien', d.msg);
        } else {
            MA.aviso('error', d.msg);
        }
        console.log('OK', d);
    },
    respuesta_err: (d) => {
        console.log('ERROR', d);
    },
    aviso: (t, m) => {
        MA.respuesta.classList.add(t);
        MA.respuesta.innerHTML = `<p>${m}</p>`;
        setTimeout(MA.limpiar_aviso, 5000);
    },
    limpiar_aviso: () => {
        MA.respuesta.innerHTML = '';
        MA.respuesta.classList = [];
    }
};

// auto-init when DOM is ready
if (document.readyState !== 'loading') {
    MenuAdmin.activar();
} else {
    document.addEventListener('DOMContentLoaded', MenuAdmin.activar);
}