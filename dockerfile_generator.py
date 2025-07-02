from flask import Flask, render_template, request, redirect, url_for, send_file
import os
import subprocess
import tempfile
import git
import shutil
import stat
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
import xml.etree.ElementTree as ET

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
GENERATED_FOLDER = 'generated'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)
os.makedirs(os.path.join('static', 'generated'), exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['GENERATED_FOLDER'] = GENERATED_FOLDER

def detect_project_type(project_path):
    for root, dirs, files in os.walk(project_path):
        if 'package.json' in files:
            with open(os.path.join(root, 'package.json')) as f:
                content = f.read()
                if 'react-scripts' in content or '"react"' in content:
                    return 'React'
            return 'Node.js'
    
        elif 'requirements.txt' in files or 'app.py' in files:
            return 'Python Flask'
        elif any(f.endswith('.csproj') for f in files):
            return '.NET'
        elif 'pom.xml' in files:
            return 'Java Maven'
        elif 'build.gradle' in files:
            return 'Java Gradle'
        elif any(f.endswith('.java') for f in files):
            return 'Java (manual)'
        elif 'vite.config.js' in files or 'vite.config.ts' in files:
            return 'Vite JS'
        elif any(f.endswith('.html') for f in files) and any(f.endswith('.js') for f in files):
            return 'Vanilla JS'

    return 'Unknown'

def detect_dotnet_dll_name(project_path):
    for file in os.listdir(project_path):
        if file.endswith(".csproj"):
            tree = ET.parse(os.path.join(project_path, file))
            root = tree.getroot()
            ns = {'msbuild': 'http://schemas.microsoft.com/developer/msbuild/2003'}
            name = root.find('msbuild:PropertyGroup/msbuild:AssemblyName', ns)
            return name.text + ".dll" if name is not None else file.replace(".csproj", ".dll")
    return "app.dll"

def build_prompt(project_type, project_path):
    base_prompt = (
        "You are a DevOps assistant. Generate a production-ready Dockerfile for the following project. "
        "The Dockerfile should include only what is needed to build and run the app successfully inside a container. "
        "Use multi-stage builds where appropriate. Always assume the container will run in a clean environment. "
        "Do not include explanations, only the Dockerfile."
    )
    if project_type == 'Node.js':
        return base_prompt + "\n\nProject type: Node.js. Use node:18-alpine. Install dependencies. Expose 3000."
    elif project_type == 'Python Flask':
        return base_prompt + """

Project type: Python Flask.

Generate a Dockerfile with the following requirements:

1. Use `python:3.11-slim` as the base image.
2. Set `WORKDIR` to `/app`.
3. Copy the full project into the container (`COPY . .`).
4. Install dependencies using `pip install -r requirements.txt`.
5. Expose port 4000 (the Flask app listens on port 4000).
6. Use `gunicorn` as the production server with the command:
   CMD ["gunicorn", "--bind", "0.0.0.0:4000", "app:app"]

Assume `app.py` exists and contains a valid `Flask` app named `app`.
"""
    elif project_type == '.NET':
        dll_name = detect_dotnet_dll_name(project_path)
        return base_prompt + f"\n\nProject type: .NET. Use SDK to publish, then run {dll_name}."
    elif project_type == 'Java Maven':
        return base_prompt + """
Project type: Java Maven.

Generate a multi-stage Dockerfile with these requirements:

1. Use `maven:3.8.6-eclipse-temurin` AS the build stage.
2. Set `WORKDIR` to `/app`.
3. Copy the full project into the build container: `COPY . .`
4. Run `mvn package -Dmaven.test.skip=true` to build the app.
5. Use `openjdk:11-jdk` as the runtime stage.
6. Set `WORKDIR` to `/app`.
7. Copy the JAR from `/app/target/*-*.jar` in the build stage to `/app/app.jar` in the runtime stage using:
   COPY --from=build-stage /app/target/*-*.jar /app/app.jar
8. Set the startup command to:
   CMD ["java", "-jar", "app.jar"]

Do **not** use ARG for JAR paths.  
Do **not** expose any ports unless the app is explicitly a web server.
"""
    elif project_type == 'Java Gradle':
        return base_prompt + "\n\nProject type: Java Gradle. Use gradle image, slim Java runtime."
    elif project_type == 'Java (manual)':
        return base_prompt + "\n\nProject type: Plain Java. Compile with javac. Run with java."
    elif project_type == 'React':
        return base_prompt + "\n\nProject type: React (frontend). Use node:18-alpine to build the app, then serve the static files using nginx. Expose port 80. Use multi-stage builds."
    elif project_type == 'Vite JS':
        return base_prompt + """
Project type: Vite-based JavaScript frontend (React, Vue, or Vanilla JS).

Generate a multi-stage Dockerfile with the following requirements:

1. Use `node:18-alpine` as the build stage.
2. Set `WORKDIR` to `/app`.
3. Copy `package.json` and `package-lock.json` (if present).
4. Run `npm install`.
5. Copy the rest of the project files.
6. Run `npm run build` to generate the production files in `dist/`.

Then:

7. Use `nginx:1.21.6-alpine` as the final image.
8. Copy the contents of `/app/dist` into `/usr/share/nginx/html`.
9. Expose port `80`.
10. Start NGINX with:
    CMD ["nginx", "-g", "daemon off;"]

Do **not** include development dependencies or source files in the final image.  
Assume the build output is inside the `dist/` directory.
"""

    else:
        return base_prompt + "\n\nProject type: Unknown. Guess based on structure."

def run_llama(prompt):
    process = subprocess.Popen(
        ["ollama", "run", "llama3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    output, _ = process.communicate(input=prompt.encode())
    return output.decode()

def extract_dockerfile_only(text):
    lines = text.strip().splitlines()
    docker_lines = []
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```dockerfile"):
            in_code_block = True
            continue
        if in_code_block and line.strip() == "```":
            break
        if in_code_block:
            docker_lines.append(line)
    if not docker_lines:
        valid_instructions = ["FROM", "WORKDIR", "COPY", "RUN", "CMD", "ENTRYPOINT", "ENV", "EXPOSE", "ARG", "LABEL", "#"]
        docker_lines = [line for line in lines if any(line.strip().startswith(i) for i in valid_instructions)]
    return "\n".join(docker_lines)
def handle_remove_readonly(func, path, exc_info):
    # Change the file to writable and retry deletion
    os.chmod(path, stat.S_IWRITE)
    func(path)

def clone_repo(url, target):
    target = os.path.normpath(target)
    if os.path.exists(target):
        shutil.rmtree(target, onerror=handle_remove_readonly)

    git.Repo.clone_from(url, target, multi_options=["--depth=1"])


def handle_remove_readonly(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def build_and_run_docker_image(project_path):
    subprocess.run(['docker', 'build', '-t', 'autogenerated-image', project_path])
    subprocess.run(['docker', 'run', '-d', '-p', '8080:80', 'autogenerated-image'])

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit', methods=['POST'])
def submit():
    try:

        repo_url = request.form.get('repo_url')
        uploaded_file = request.files.get('project_folder')
        run_container = request.form.get('run_container') == 'on'
        project_path = None
        github_user = None
        github_avatar = None

        if repo_url:
            parsed = urlparse(repo_url)
            path_parts = parsed.path.strip("/").split("/")
            if len(path_parts) >= 2:
                github_user = path_parts[0]
                github_avatar = f"https://github.com/{github_user}.png"


        if uploaded_file:
            filename = secure_filename(uploaded_file.filename)
            saved_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(saved_path)
            extract_path = os.path.splitext(saved_path)[0]
            shutil.unpack_archive(saved_path, extract_path)
            project_path = extract_path

        elif repo_url:
            repo_name = os.path.splitext(os.path.basename(urlparse(repo_url).path))[0]
            repo_path = os.path.join(app.config['UPLOAD_FOLDER'], repo_name)
            clone_repo(repo_url, repo_path)
            project_path = repo_path

        if project_path:
                project_type = detect_project_type(project_path)
                prompt = build_prompt(project_type, project_path)
                response = run_llama(prompt)
                dockerfile_content = extract_dockerfile_only(response)

                dockerfile_path = os.path.join(project_path, 'Dockerfile')
                with open(dockerfile_path, 'w') as f:
                    f.write(dockerfile_content)

                if run_container:
                    try:
                        build_and_run_docker_image(project_path)
                    except Exception as e:
                        print(f"⚠️ Docker run failed: {e}")

                public_path = os.path.join('static', 'generated', os.path.basename(dockerfile_path))
                shutil.copy(dockerfile_path, public_path)

                return render_template('success.html',
                    dockerfile_name=os.path.basename(dockerfile_path),
                    dockerfile_url=url_for('static', filename=f'generated/{os.path.basename(dockerfile_path)}'),
                    container_started=run_container,
                    dockerfile_content=dockerfile_content,
                    github_user=github_user,
                    github_avatar=github_avatar
                )

        return redirect(url_for('index'))

    except Exception as e:
        print(f"💥 Unhandled error: {e}")
        return render_template('error.html', error=str(e)), 500

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
