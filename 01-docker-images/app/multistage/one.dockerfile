FROM eclipse-temurin:21-jdk
WORKDIR /app
COPY Main.java .
RUN javac Main.java \
    && jar --create --file app.jar --main-class Main Main.class
CMD ["java", "-jar", "app.jar"]
