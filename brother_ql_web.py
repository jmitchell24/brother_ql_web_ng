#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse, re, base64, os
from io import BytesIO

from bottle import run, route, get, post, delete, response, request, jinja2_view as view, static_file, redirect, BaseRequest, abort
BaseRequest.MEMFILE_MAX = 16 * 1024 * 1024  # 16 MB — needed for image uploads
from PIL import Image, ImageDraw, ImageFont

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

from font_helpers import get_fonts

logger = logging.getLogger(__name__)

LABEL_SIZES = [ (name, label_type_specs[name]['name']) for name in label_sizes]

try:
    with open('config.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)


@route('/')
def index():
    redirect('/labeldesigner')

@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')

@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_family_names = sorted(list(FONTS.keys()))
    return {'font_family_names': font_family_names,
            'fonts': FONTS,
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}

def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode() # UTF-8 decoded form data

    font_family = d.get('font_family').rpartition('(')[0].strip()
    font_style  = d.get('font_family').rpartition('(')[2].rstrip(')')
    context = {
      'text':          d.get('text', None),
      'font_size': int(d.get('font_size', 100)),
      'font_family':   font_family,
      'font_style':    font_style,
      'label_size':    d.get('label_size', "62"),
      'kind':          label_type_specs[d.get('label_size', "62")]['kind'],
      'margin':    int(d.get('margin', 10)),
      'threshold': int(d.get('threshold', 70)),
      'align':         d.get('align', 'center'),
      'orientation':   d.get('orientation', 'standard'),
      'margin_top':    float(d.get('margin_top',    24))/100.,
      'margin_bottom': float(d.get('margin_bottom', 45))/100.,
      'margin_left':   float(d.get('margin_left',   35))/100.,
      'margin_right':  float(d.get('margin_right',  35))/100.,
      'border':        d.get('border', '') == 'true',
      'border_width':  int(d.get('border_width', 3)),
      'image_data':    d.get('image_data', '') or None,
      'image_bw':      d.get('image_bw', 'true') == 'true',
      'image_align':   d.get('image_align', 'left'),
      'image_gap':     int(d.get('image_gap', 20)),
    }
    context['margin_top']    = int(context['font_size']*context['margin_top'])
    context['margin_bottom'] = int(context['font_size']*context['margin_bottom'])
    context['margin_left']   = int(context['font_size']*context['margin_left'])
    context['margin_right']  = int(context['font_size']*context['margin_right'])

    # Border width is added to margins so text stays clear of the border
    if context['border']:
        bw = context['border_width']
        context['margin_top']    += bw
        context['margin_bottom'] += bw
        context['margin_left']   += bw
        context['margin_right']  += bw

    context['fill_color']  = (255, 0, 0) if 'red' in context['label_size'] else (0, 0, 0)

    def get_font_path(font_family_name, font_style_name):
        try:
            if font_family_name is None or font_style_name is None:
                font_family_name = CONFIG['LABEL']['DEFAULT_FONTS']['family']
                font_style_name =  CONFIG['LABEL']['DEFAULT_FONTS']['style']
            font_path = FONTS[font_family_name][font_style_name]
        except KeyError:
            raise LookupError("Couln't find the font & style")
        return font_path

    context['font_path'] = get_font_path(context['font_family'], context['font_style'])

    def get_label_dimensions(label_size):
        try:
            ls = label_type_specs[context['label_size']]
        except KeyError:
            raise LookupError("Unknown label_size")
        return ls['dots_printable']

    width, height = get_label_dimensions(context['label_size'])
    if height > width: width, height = height, width
    if context['orientation'] == 'rotated': height, width = width, height
    context['width'], context['height'] = width, height

    return context

def create_label_im(text, **kwargs):
    label_type  = kwargs['kind']
    im_font     = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    fill_color  = kwargs['fill_color']
    orientation = kwargs['orientation']

    # Normalise empty lines in text
    text = '\n'.join(line if line else ' ' for line in text.split('\n'))

    # Measure text on a scratch canvas
    tmp  = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(tmp)
    tb   = draw.multiline_textbbox((0, 0), text, font=im_font)
    textsize = (tb[2] - tb[0], tb[3] - tb[1])

    width, height = kwargs['width'], kwargs['height']
    gap = kwargs['image_gap']

    # --- Load and process image ---
    label_img = None
    img_w = img_h = 0
    if kwargs.get('image_data'):
        raw = base64.b64decode(kwargs['image_data'])
        pil = Image.open(BytesIO(raw))
        # Flatten onto white background
        flat = Image.new('RGB', pil.size, 'white')
        if pil.mode == 'RGBA':
            flat.paste(pil, mask=pil.split()[3])
        else:
            flat.paste(pil.convert('RGB'))
        if kwargs['image_bw']:
            flat = flat.convert('L').convert('RGB')
        label_img = flat

    # --- Determine label canvas size ---
    if orientation == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif orientation == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']

    # --- Scale image to content height ---
    if label_img:
        content_h = height - kwargs['margin_top'] - kwargs['margin_bottom']
        content_h = max(content_h, 1)
        scale = content_h / label_img.height
        img_w = max(1, int(label_img.width * scale))
        img_h = content_h
        label_img = label_img.resize((img_w, img_h), Image.LANCZOS)

    # --- Create final canvas ---
    im   = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)

    # --- Layout ---
    if orientation == 'standard':
        # Centre image vertically in the full label (matches how text is centred on die-cut labels)
        img_y = (height - img_h) // 2 if img_h else kwargs['margin_top']

        if label_img:
            img_align = kwargs['image_align']
            if img_align == 'left':
                img_x    = kwargs['margin_left']
                text_x   = img_x + img_w + gap
                text_area_w = width - text_x - kwargs['margin_right']
            else:
                img_x    = width - kwargs['margin_right'] - img_w
                text_x   = kwargs['margin_left']
                text_area_w = img_x - gap - text_x

            text_area_w = max(text_area_w, 0)
            im.paste(label_img, (img_x, img_y))

            if kwargs['align'] == 'center':
                horizontal_offset = text_x + max((text_area_w - textsize[0]) // 2, 0)
            elif kwargs['align'] == 'right':
                horizontal_offset = text_x + max(text_area_w - textsize[0], 0)
            else:
                horizontal_offset = text_x
        else:
            horizontal_offset = max((width - textsize[0]) // 2, 0)

        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1]) // 2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom']) // 2
        else:
            vertical_offset = kwargs['margin_top']

    elif orientation == 'rotated':
        vertical_offset  = (height - textsize[1]) // 2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom']) // 2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0]) // 2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
        if label_img:
            im.paste(label_img, (kwargs['margin_left'], kwargs['margin_top']))

    draw.multiline_text((horizontal_offset, vertical_offset), text, fill_color, font=im_font, align=kwargs['align'])

    if kwargs.get('border'):
        bw = kwargs['border_width']
        draw.rectangle([(bw//2, bw//2), (width - bw//2 - 1, height - bw//2 - 1)],
                       outline=fill_color, width=bw)
    return im

USB_SPEEDS = {1: 'Low Speed (1.5 Mbit/s)', 2: 'Full Speed (12 Mbit/s)', 3: 'High Speed (480 Mbit/s)'}

@get('/api/printer/info')
def printer_info():
    response.content_type = 'application/json'
    info = {
        'model':   CONFIG['PRINTER']['MODEL'],
        'printer': CONFIG['PRINTER']['PRINTER'],
        'label':   CONFIG['LABEL']['DEFAULT_SIZE'],
    }
    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
        if selected_backend == 'pyusb':
            import usb.core
            m = re.match(r'usb://(\w+):(\w+)', CONFIG['PRINTER']['PRINTER'])
            if m:
                dev = usb.core.find(idVendor=int(m.group(1), 16), idProduct=int(m.group(2), 16))
                if dev:
                    info['status']       = 'online'
                    info['manufacturer'] = dev.manufacturer
                    info['product']      = dev.product
                    info['serial']       = dev.serial_number
                    info['usb_bus']      = dev.bus
                    info['usb_address']  = dev.address
                    info['usb_speed']    = USB_SPEEDS.get(dev.speed, 'Unknown')
                else:
                    info['status'] = 'offline'
            else:
                info['status'] = 'unknown'
        elif selected_backend == 'linux_kernel':
            import os
            path = CONFIG['PRINTER']['PRINTER'].replace('file://', '')
            info['status'] = 'online' if os.path.exists(path) else 'offline'
        else:
            info['status'] = 'unknown'
    except Exception as e:
        info['status'] = 'error'
        info['error']  = str(e)
    return json.dumps(info)

@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)

def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()

@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = str(e)
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    red = False
    if 'red' in context['label_size']:
        red = True
    create_label(qlr, im, context['label_size'], red=red, threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

SAVED_CONFIGS_FILE = 'saved_configs.json'

def _load_saved_configs():
    if not os.path.exists(SAVED_CONFIGS_FILE):
        return {}
    try:
        with open(SAVED_CONFIGS_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}

def _save_configs(configs):
    with open(SAVED_CONFIGS_FILE, 'w', encoding='utf-8') as fh:
        json.dump(configs, fh, ensure_ascii=False, indent=2)

@get('/api/configs')
def list_configs():
    response.content_type = 'application/json'
    configs = _load_saved_configs()
    return json.dumps([{'name': name, 'saved_at': v.get('saved_at', '')} for name, v in sorted(configs.items())])

@post('/api/configs/<name>')
def save_config(name):
    response.content_type = 'application/json'
    if not name or len(name) > 80:
        abort(400, 'Invalid config name')
    data = request.json
    if not isinstance(data, dict):
        abort(400, 'Expected JSON object')
    from datetime import datetime
    configs = _load_saved_configs()
    data['saved_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    configs[name] = data
    _save_configs(configs)
    return json.dumps({'success': True})

@get('/api/configs/<name>')
def load_config(name):
    response.content_type = 'application/json'
    configs = _load_saved_configs()
    if name not in configs:
        abort(404, 'Config not found')
    return json.dumps(configs[name])

@delete('/api/configs/<name>')
def delete_config(name):
    response.content_type = 'application/json'
    configs = _load_saved_configs()
    if name not in configs:
        abort(404, 'Config not found')
    del configs[name]
    _save_configs(configs)
    return json.dumps({'success': True})

@delete('/api/configs')
def delete_all_configs():
    response.content_type = 'application/json'
    _save_configs({})
    return json.dumps({'success': True})

def main():
    global DEBUG, FONTS, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', default=False, help='folder for additional .ttf/.otf fonts')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 62.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        ADDITIONAL_FONT_FOLDER = args.font_folder
    else:
        ADDITIONAL_FONT_FOLDER = CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']


    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    FONTS = get_fonts()
    if ADDITIONAL_FONT_FOLDER:
        FONTS.update(get_fonts(ADDITIONAL_FONT_FOLDER))

    if not FONTS:
        sys.stderr.write("Not a single font was found on your system. Please install some or use the \"--font-folder\" argument.\n")
        sys.exit(2)

    for font in CONFIG['LABEL']['DEFAULT_FONTS']:
        try:
            FONTS[font['family']][font['style']]
            CONFIG['LABEL']['DEFAULT_FONTS'] = font
            logger.debug("Selected the following default font: {}".format(font))
            break
        except: pass
    if CONFIG['LABEL']['DEFAULT_FONTS'] is None:
        sys.stderr.write('Could not find any of the default fonts. Choosing a random one.\n')
        family =  random.choice(list(FONTS.keys()))
        style =   random.choice(list(FONTS[family].keys()))
        CONFIG['LABEL']['DEFAULT_FONTS'] = {'family': family, 'style': style}
        sys.stderr.write('The default font is now set to: {family} ({style})\n'.format(**CONFIG['LABEL']['DEFAULT_FONTS']))

    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()
