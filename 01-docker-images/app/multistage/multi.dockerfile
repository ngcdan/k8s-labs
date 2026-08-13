FROM eclipse-temurin:21-jdk AS build
WORKDIR /app
COPY Main.java .
RUN javac Main.java \
    && jar --create --file app.jar --main-class Main Main.class

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=build /app/app.jar .
CMD ["java", "-jar", "app.jar"]
