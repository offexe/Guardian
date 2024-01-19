from flask import Flask, request, redirect, render_template
from PIL import Image
import base64
import io
import requests
from utils import resize_and_crop
from AI_lib import encoder_attack , diffusion_attack, diffusion, styleGan,get_target
app = Flask(__name__)

encoded_image='' #the user image in bytes
encoded_mask='' #the user mask in  bytes
encoded_target='' #the user target in byets
file_img=''
image_orginal='' #the user image
image_mask='' #the user mask
image_target='' #the user target image

'''home page, page 1'''
@app.route('/')
def home():
    '''show the page'''
    return render_template('index.html')

'''mask page, page 2'''
@app.route('/mask', methods=['POST'])
def upload_image():
    '''get the image from user and save it
       after that go that to step2 page'''
    global encoded_image, image_orginal
    file = request.files['file']
    im = Image.open(file.stream)
    im =resize_and_crop(im, (512,512), crop_type="center")
    data = io.BytesIO()
    image_orginal=im
    im.save(data, "PNG")
    encoded_image=base64.b64encode(data.getvalue())
    return render_template('mask.html',img_data=encoded_image.decode('utf-8'))

'''settings page, page 3'''
@app.route('/settings', methods=['POST'])
def process_canvas():
    '''get the mask the user created and save it
       after that go that to settings page, page 3'''
    global encoded_mask,image_mask
    canvas_data = request.form.get('canvasData')
    img_data = canvas_data.split(',')[1]  # Remove the "data:image/png;base64," prefix
    img_bytes = io.BytesIO(base64.b64decode(img_data))
    im = Image.open(img_bytes)
    data = io.BytesIO()
    im.save(data, "PNG")
    encoded_mask=base64.b64encode(data.getvalue())
    image_mask=im
    return render_template('settings.html',img_before=encoded_image.decode('utf-8'),mask_im=encoded_mask.decode('utf-8'),target_im='https://htmlcolorcodes.com/assets/images/colors/blue-color-solid-background-1920x1080.png')

'''results page, page 4'''
@app.route('/results', methods=['POST'])
def process():
    '''get promt,AI moudle,attack,and taget from user
    then run the needed functios and return images to user'''
    images=[{"name":"orginal image","img":encoded_image.decode('utf-8')}]
    promt = request.form.get('promt')
    neutral_input=request.form.get('neutral')
    diffusion_check= request.form.get('Diffusion')
    styleGan_check= request.form.get('styleGan')
    No_immunization_check= request.form.get('No immunization')
    Encoder_attack_check= request.form.get('Encoder_attack')
    Diffusion_attack_check= request.form.get('Diffusion_attack')
    if (Encoder_attack_check or Diffusion_attack_check):
        image_target_url = request.form.get('selected_target')
        # Use requests to download the image from the URL
        response = requests.get(image_target_url)
        image_data = io.BytesIO(response.content)        
        image_target = Image.open(image_data) 
    if(Encoder_attack_check):
        Encoder_attack_image=encoder_attack(image_orginal,image_mask,image_target)
        img=image_to_html(Encoder_attack_image)
        images.append({"name":"encoder immunization","img":img})
    if(Diffusion_attack_check):
        Diffusion_attack_image=diffusion_attack(image_orginal,image_mask,image_target)
        img=image_to_html(Diffusion_attack_image)
        images.append({"name":"diffusion immunization","img":img})
    if(diffusion_check):
        if(No_immunization_check):
            No_immunization_diffusion_image=diffusion(image_orginal,image_mask,promt)
            img=image_to_html(No_immunization_diffusion_image)
            images.append({"name":"diffusion model on no immunization","img":img})
        if(Encoder_attack_check):
            Encoder_attack_diffusion_image=diffusion(Encoder_attack_image,image_mask,promt)
            img=image_to_html(Encoder_attack_diffusion_image)
            images.append({"name":"diffusion model on encoder immunization","img":img})
        if(Diffusion_attack_check):
            Diffusion_attack_diffusion_image=diffusion(Diffusion_attack_image,image_mask,promt)
            img=image_to_html(Diffusion_attack_diffusion_image)
            images.append({"name":"diffusion model on diffusion immunization","img":img})
    if(styleGan_check):
        if(No_immunization_check):
            No_immunization_stylegan_image=styleGan(image_orginal,neutral_input,promt)
            img=image_to_html(No_immunization_stylegan_image)
            images.append({"name":"stylegan on no immunization","img":img})
        if(Encoder_attack_check):
            Encoder_attack_stylegan_image=styleGan(Encoder_attack_image,neutral_input,promt)
            img=image_to_html(Encoder_attack_stylegan_image)
            images.append({"name":"stylegan on Encoder attack","img":img})
        if(Diffusion_attack_check):
            Diffusion_attack_stylegan_image=styleGan(Diffusion_attack_image,neutral_input,promt)
            img=image_to_html(Diffusion_attack_stylegan_image)
            images.append({"name":"stylegan on Diffusion attack","img":img})
    return render_template('results.html',images_list=images)

def image_to_html(image):
    data = io.BytesIO()
    image.save(data, "PNG")
    encoded=base64.b64encode(data.getvalue())
    return encoded.decode('utf-8')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)
